#!/bin/bash

WORK_DIR="./tmp"
rm -r ${WORK_DIR}
mkdir -p ${WORK_DIR}

PROJECT_NAME=${PROJECT_NAME:-"dftd3"}

SOURCE_URL=${SOURCE_URL:-"https://github.com/dftd3/simple-dftd3/releases/download/v1.0.0/dftd3-1.0.0-sdist.tar.gz"}

TAR_GZ_NAME=$(basename ${SOURCE_URL})

BUILD_DIR="${WORK_DIR}/_build"
INSTALL_DIR="${WORK_DIR}/${PROJECT_NAME}-build"

pip3 install meson ninja

cd ${WORK_DIR}

echo "Downloading source code from $SOURCE_URL..."
curl -L $SOURCE_URL -o $TAR_GZ_NAME

echo "Extracting $TAR_GZ_NAME..."
tar -xzf $TAR_GZ_NAME

SOURCE_DIR=$(tar -tf $TAR_GZ_NAME | head -1 | cut -f1 -d"/")
cd $SOURCE_DIR

echo "
option(
  'openmp',
  type: 'boolean',
  value: false,
  yield: true,
  description: 'Use OpenMP parallelisation',
)" >> meson_options.txt

echo "Setting up build system with meson..."
meson setup --wipe $BUILD_DIR -Dopenmp=false

echo "Compiling the code..."
meson compile -C $BUILD_DIR

echo "Configuring build system with prefix..."
meson configure $BUILD_DIR --prefix=$(realpath ${INSTALL_DIR})

echo "Installing to $INSTALL_DIR..."
meson install -C $BUILD_DIR

echo "Installation complete."

cd ../../

echo "All operations completed."