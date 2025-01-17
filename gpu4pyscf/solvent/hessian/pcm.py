# Copyright 2023 The GPU4PySCF Authors. All Rights Reserved.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

'''
Gradient of PCM family solvent model
'''
# pylint: disable=C0103

import numpy
import cupy
from cupyx import scipy
from pyscf import lib
from pyscf import gto, df
from pyscf.grad import rhf as rhf_grad
from gpu4pyscf.solvent.pcm import PI, switch_h
from gpu4pyscf.solvent.grad.pcm import grad_switch_h, get_dF_dA, get_dD_dS, grad_qv, grad_solver, grad_nuc
from gpu4pyscf.df import int3c2e
from gpu4pyscf.lib.cupy_helper import contract
from gpu4pyscf.lib import logger

libdft = lib.load_library('libdft')

def hess_nuc(pcmobj):
    if not pcmobj._intermediates:
        pcmobj.build()
    mol = pcmobj.mol
    q_sym        = pcmobj._intermediates['q_sym'].get()
    gridslice    = pcmobj.surface['gslice_by_atom']
    grid_coords  = pcmobj.surface['grid_coords'].get()
    exponents    = pcmobj.surface['charge_exp'].get()

    atom_coords = mol.atom_coords(unit='B')
    atom_charges = numpy.asarray(mol.atom_charges(), dtype=numpy.float64)
    fakemol_nuc = gto.fakemol_for_charges(atom_coords)
    fakemol = gto.fakemol_for_charges(grid_coords, expnt=exponents**2)

    # nuclei potential response
    int2c2e_ip1ip2 = mol._add_suffix('int2c2e_ip1ip2')
    v_ng_ip1ip2 = gto.mole.intor_cross(int2c2e_ip1ip2, fakemol_nuc, fakemol).reshape([3,3,mol.natm,-1])
    dv_g = numpy.einsum('n,xyng->ngxy', atom_charges, v_ng_ip1ip2)
    dv_g = numpy.einsum('ngxy,g->ngxy', dv_g, q_sym)

    de = numpy.zeros([mol.natm, mol.natm, 3, 3])
    for ia in range(mol.natm):
        p0, p1 = gridslice[ia]
        de_tmp = numpy.sum(dv_g[:,p0:p1], axis=1)
        de[:,ia] -= de_tmp
        #de[ia,:] -= de_tmp.transpose([0,2,1])


    int2c2e_ip1ip2 = mol._add_suffix('int2c2e_ip1ip2')
    v_ng_ip1ip2 = gto.mole.intor_cross(int2c2e_ip1ip2, fakemol, fakemol_nuc).reshape([3,3,-1,mol.natm])
    dv_g = numpy.einsum('n,xygn->gnxy', atom_charges, v_ng_ip1ip2)
    dv_g = numpy.einsum('gnxy,g->gnxy', dv_g, q_sym)

    for ia in range(mol.natm):
        p0, p1 = gridslice[ia]
        de_tmp = numpy.sum(dv_g[p0:p1], axis=0)
        de[ia,:] -= de_tmp
        #de[ia,:] -= de_tmp.transpose([0,2,1])

    int2c2e_ipip1 = mol._add_suffix('int2c2e_ipip1')
    v_ng_ipip1 = gto.mole.intor_cross(int2c2e_ipip1, fakemol_nuc, fakemol).reshape([3,3,mol.natm,-1])
    dv_g = numpy.einsum('g,xyng->nxy', q_sym, v_ng_ipip1)
    for ia in range(mol.natm):
        de[ia,ia] -= dv_g[ia] * atom_charges[ia]

    v_ng_ipip1 = gto.mole.intor_cross(int2c2e_ipip1, fakemol, fakemol_nuc).reshape([3,3,-1,mol.natm])
    dv_g = numpy.einsum('n,xygn->gxy', atom_charges, v_ng_ipip1)
    dv_g = numpy.einsum('g,gxy->gxy', q_sym, dv_g)
    for ia in range(mol.natm):
        p0, p1 = gridslice[ia]
        de[ia,ia] -= numpy.sum(dv_g[p0:p1], axis=0)

    return de

def hess_qv(pcmobj, dm, verbose=None):
    if not pcmobj._intermediates or 'q_sym' not in pcmobj._intermediates:
        pcmobj._get_vind(dm)
    gridslice    = pcmobj.surface['gslice_by_atom']
    q_sym        = pcmobj._intermediates['q_sym']

    intopt = pcmobj.intopt
    intopt.clear()
    # rebuild with aosym
    intopt.build(1e-14, diag_block_with_triu=True, aosym=False)
    coeff = intopt.coeff
    dm_cart = coeff @ dm @ coeff.T
    #dm_cart = cupy.einsum('pi,ij,qj->pq', coeff, dm, coeff)

    dvj, _ = int3c2e.get_int3c2e_ipip1_hjk(intopt, q_sym, None, dm_cart, with_k=False)
    dq, _ = int3c2e.get_int3c2e_ipvip1_hjk(intopt, q_sym, None, dm_cart, with_k=False)
    dvj, _ = int3c2e.get_int3c2e_ip1ip2_hjk(intopt, q_sym, None, dm_cart, with_k=False)
    dq, _ = int3c2e.get_int3c2e_ipip2_hjk(intopt, q_sym, None, dm_cart, with_k=False)

    cart_ao_idx = intopt.cart_ao_idx
    rev_cart_ao_idx = numpy.argsort(cart_ao_idx)
    dvj = dvj[:,rev_cart_ao_idx]

    aoslice = intopt.mol.aoslice_by_atom()
    dq = cupy.asarray([cupy.sum(dq[:,p0:p1], axis=1) for p0,p1 in gridslice])
    dvj= 2.0 * cupy.asarray([cupy.sum(dvj[:,p0:p1], axis=1) for p0,p1 in aoslice[:,2:]])
    de = dq + dvj
    return de.get()

def hess_elec(pcmobj, dm, verbose=None):
    '''
    slow version with finite difference
    TODO: use analytical hess_nuc
    '''
    log = logger.new_logger(pcmobj, verbose)
    t1 = log.init_timer()
    pmol = pcmobj.mol.copy()
    mol = pmol.copy()
    coords = mol.atom_coords(unit='Bohr')

    def pcm_grad_scanner(mol):
        # TODO: use more analytical forms
        pcmobj.reset(mol)
        e, v = pcmobj._get_vind(dm)
        #return grad_elec(pcmobj, dm)
        return grad_nuc(pcmobj, dm) + grad_solver(pcmobj, dm) + grad_qv(pcmobj, dm)
    mol.verbose = 0
    de = numpy.zeros([mol.natm, mol.natm, 3, 3])
    eps = 1e-3
    for ia in range(mol.natm):
        for ix in range(3):
            dv = numpy.zeros_like(coords)
            dv[ia,ix] = eps
            mol.set_geom_(coords + dv, unit='Bohr')
            mol.build()
            g0 = pcm_grad_scanner(mol)

            mol.set_geom_(coords - dv, unit='Bohr')
            mol.build()
            g1 = pcm_grad_scanner(mol)
            de[ia,:,ix] = (g0 - g1)/2.0/eps
    t1 = log.timer_debug1('solvent energy', *t1)
    pcmobj.reset(pmol)
    return de

def fd_grad_vmat(pcmobj, mo_coeff, mo_occ, atmlst=None, verbose=None):
    '''
    dv_solv / da
    slow version with finite difference
    '''
    log = logger.new_logger(pcmobj, verbose)
    t1 = log.init_timer()
    pmol = pcmobj.mol.copy()
    mol = pmol.copy()
    if atmlst is None:
        atmlst = range(mol.natm)
    nao, nmo = mo_coeff.shape
    mocc = mo_coeff[:,mo_occ>0]
    nocc = mocc.shape[1]
    dm = cupy.dot(mocc, mocc.T) * 2
    coords = mol.atom_coords(unit='Bohr')
    def pcm_vmat_scanner(mol):
        pcmobj.reset(mol)
        e, v = pcmobj._get_vind(dm)
        return v

    mol.verbose = 0
    vmat = cupy.empty([len(atmlst), 3, nao, nocc])
    eps = 1e-3
    for i0, ia in enumerate(atmlst):
        for ix in range(3):
            dv = numpy.zeros_like(coords)
            dv[ia,ix] = eps
            mol.set_geom_(coords + dv, unit='Bohr')
            mol.build()
            vmat0 = pcm_vmat_scanner(mol)

            mol.set_geom_(coords - dv, unit='Bohr')
            mol.build()
            vmat1 = pcm_vmat_scanner(mol)

            grad_vmat = (vmat0 - vmat1)/2.0/eps
            grad_vmat = contract("ij,jq->iq", grad_vmat, mocc)
            grad_vmat = contract("iq,ip->pq", grad_vmat, mo_coeff)
            vmat[i0,ix] = grad_vmat
    t1 = log.timer_debug1('computing solvent grad veff', *t1)
    pcmobj.reset(pmol)
    return vmat

"""
def analytic_grad_vmat(pcmobj, mo_coeff, mo_occ, atmlst=None, verbose=None):
    '''
    dv_solv / da
    slow version with finite difference
    '''
    log = logger.new_logger(pcmobj, verbose)
    t1 = log.init_timer()
    pmol = pcmobj.mol.copy()
    mol = pmol.copy()
    if atmlst is None:
        atmlst = range(mol.natm)
    nao, nmo = mo_coeff.shape
    mocc = mo_coeff[:,mo_occ>0]
    nocc = mocc.shape[1]
    dm = cupy.dot(mocc, mocc.T) * 2
    coords = mol.atom_coords(unit='Bohr')

    # TODO: add those contributions
    # contribution due to _get_v
    # contribution due to linear solver
    # contribution due to _get_vmat

    vmat = cupy.zeros([len(atmlst), 3, nao, nocc])

    t1 = log.timer_debug1('computing solvent grad veff', *t1)
    pcmobj.reset(pmol)
    return vmat
"""
def make_hess_object(hess_method):
    '''
    return solvent hessian object
    '''
    hess_method_class = hess_method.__class__
    class WithSolventHess(hess_method_class):
        def __init__(self, hess_method):
            self.__dict__.update(hess_method.__dict__)
            self.de_solvent = None
            self.de_solute = None
            self._keys = self._keys.union(['de_solvent', 'de_solute'])

        def kernel(self, *args, dm=None, atmlst=None, **kwargs):
            dm = kwargs.pop('dm', None)
            if dm is None:
                dm = self.base.make_rdm1(ao_repr=True)
            is_equilibrium = self.base.with_solvent.equilibrium_solvation
            self.base.with_solvent.equilibrium_solvation = True
            self.de_solvent = hess_elec(self.base.with_solvent, dm, verbose=self.verbose)
            #self.de_solvent+= hess_nuc(self.base.with_solvent)
            self.de_solute = hess_method_class.kernel(self, *args, **kwargs)
            self.de = self.de_solute + self.de_solvent
            self.base.with_solvent.equilibrium_solvation = is_equilibrium
            return self.de

        def make_h1(self, mo_coeff, mo_occ, chkfile=None, atmlst=None, verbose=None):
            if atmlst is None:
                atmlst = range(self.mol.natm)
            h1ao = hess_method_class.make_h1(self, mo_coeff, mo_occ, atmlst=atmlst, verbose=verbose)
            dv = fd_grad_vmat(self.base.with_solvent, mo_coeff, mo_occ, atmlst=atmlst, verbose=verbose)
            for i0, ia in enumerate(atmlst):
                h1ao[i0] += dv[i0]
            return h1ao

        def _finalize(self):
            # disable _finalize. It is called in grad_method.kernel method
            # where self.de was not yet initialized.
            pass

    return WithSolventHess(hess_method)

