# syntax=docker/dockerfile:1.7
#
# The heavy-solver image (#339): every external solver the Linux heavy-solves lane
# exercises, prebuilt, so that lane can run on a GitHub-hosted runner (#338) instead
# of a self-hosted box that accumulated hours of source builds.
#
# Provisioning goes through scripts/install-solvers.sh, tools/build_openinjmoldsim.sh
# and scripts/install-freecad-appimage.sh wherever those scripts execute a build, so
# the image is also a standing test of the documented install path. Where a script
# only PRINTS its recipe (the FSI stack), the stage below runs that recipe verbatim.
#
# Source builds are pinned to the revisions the self-hosted Linux runner validated
# nightly before this image existed — the combination with a green history.
#
# Stages (each source build is independent, so CI can build and cache them in
# parallel jobs, each well under a hosted runner's 6-hour cap):
#   system  apt: toolchain, ESI OpenFOAM 2512 (+dev), Elmer, CalculiX, PrusaSlicer, dev libs
#   yade    install-solvers.sh dem_gpl     -> /opt/yade
#   openems install-solvers.sh em_gpl      -> /opt/openEMS + /opt/venv-openems
#   fsi     serial preCICE + calculix-adapter + openfoam-adapter -> /opt/fsi
#   oims    tools/build_openinjmoldsim.sh  -> /opt/of7 (OpenFOAM-7 .org + openInjMoldSim)
#   final   system + the four prefixes + FreeCAD AppImage + Bempp venv + driver venv
#
# Build locally:  docker build -f docker/heavy-solvers/Dockerfile --build-arg JOBS=8 .
#
# Licences of the bundled components: docker/heavy-solvers/LICENSES.md.

ARG UBUNTU=ubuntu:24.04

# ---------------------------------------------------------------------------------
FROM ${UBUNTU} AS system
ARG DEBIAN_FRONTEND=noninteractive
# OpenFOAM's etc/bashrc derives the user dir from $USER, which docker leaves unset.
ENV USER=root LANG=C.UTF-8
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget gnupg software-properties-common git bzip2 unzip file \
      python3 python3-venv python3-dev python3-pip \
      build-essential cmake gfortran pkg-config ccache flex bison \
 && curl -fsSL https://dl.openfoam.com/add-debian-repo.sh | bash \
 && add-apt-repository -y ppa:elmer-csc-ubuntu/elmer-csc-ppa \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      openfoam2512-dev \
      elmerfem-csc calculix-ccx prusa-slicer \
      # YADE (the dem_gpl list in install-solvers.sh)
      libboost-all-dev libgmp-dev libmpfr-dev libcgal-dev libeigen3-dev \
      python3-numpy python3-mpmath libvtk9-dev libgts-dev libmetis-dev \
      libopenblas-dev libsuitesparse-dev zlib1g-dev \
      ipython3 python3-matplotlib python3-pygraphviz python3-xlib python3-sphinx python3-tk \
      # openEMS (the em_gpl list), GUI disabled
      libhdf5-dev libtinyxml-dev \
      # FSI (the fsi list) + calculix-adapter's yaml-cpp
      libxml2-dev libspooles-dev libarpack2-dev liblapack-dev libblas-dev libyaml-cpp-dev \
      # OpenFOAM-7 (.org) build
      libreadline-dev libncurses-dev libxt-dev libscotch-dev libptscotch-dev \
      libopenmpi-dev openmpi-bin \
      # extracted FreeCAD AppImage runtime (headless freecadcmd)
      libgl1 libglib2.0-0t64 libfontconfig1 libxkbcommon0 libdbus-1-3
WORKDIR /src

# ---------------------------------------------------------------------------------
FROM system AS yade
ARG JOBS=4
ARG YADE_SHA=a48efeefb6318fc82d474f36aa2d97697f0adf8c
COPY scripts/install-solvers.sh /src/ankusdrive/scripts/install-solvers.sh
# Pre-clone at the pin; install-solvers.sh skips its own clone when .git exists.
RUN git init -q /src/yade && cd /src/yade \
 && git remote add origin https://gitlab.com/yade-dev/trunk.git \
 && git fetch -q --depth 1 origin "$YADE_SHA" && git checkout -q FETCH_HEAD \
 && YADE_PREFIX=/opt/yade YADE_SRC=/src/yade YADE_JOBS="$JOBS" \
      bash /src/ankusdrive/scripts/install-solvers.sh dem_gpl \
 && /opt/yade/bin/yade --version

# ---------------------------------------------------------------------------------
FROM system AS openems
ARG JOBS=4
ARG OPENEMS_SHA=112b5f492c1a3a08ff69338d7ef39a0cf3a5b7bc
COPY scripts/install-solvers.sh /src/ankusdrive/scripts/install-solvers.sh
RUN git clone -q https://github.com/thliebig/openEMS-Project.git /src/openEMS-Project \
 && cd /src/openEMS-Project && git checkout -q "$OPENEMS_SHA" \
 && git submodule update -q --init --recursive \
 && EM_PREFIX=/opt/openEMS EM_SRC=/src/openEMS-Project EM_VENV=/opt/venv-openems \
    EM_UPDATE_FLAGS="--disable-GUI --njobs=$JOBS" \
      bash /src/ankusdrive/scripts/install-solvers.sh em_gpl

# ---------------------------------------------------------------------------------
# install-solvers.sh `fsi` prints this recipe rather than running it; this stage is
# that recipe with container paths. Keep the two in step.
FROM system AS fsi
ARG JOBS=4
ARG PRECICE_TAG=v3.4.0
ARG CCX_ADAPTER_SHA=ff3950b9a23925d223d4cb45606272cbf57d626f
ARG OF_ADAPTER_SHA=f6d7928c52df4bf109a4d5abd245531d6eeb136c
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
# 1) serial libprecice (MPI OFF — see the conda-forge MPI double-load gotcha in the script)
RUN git clone -q --depth 1 --branch "$PRECICE_TAG" https://github.com/precice/precice.git /src/precice \
 && cmake -S /src/precice -B /src/precice/build -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX=/opt/fsi/precice -DPRECICE_FEATURE_MPI_COMMUNICATION=OFF \
      -DPRECICE_FEATURE_PETSC_MAPPING=OFF -DPRECICE_FEATURE_PYTHON_ACTIONS=OFF \
      -DBUILD_TESTING=OFF \
 && cmake --build /src/precice/build -j"$JOBS" && cmake --install /src/precice/build
# 2) CalculiX 2.20 source + calculix-adapter -> ccx_preCICE
RUN wget -q http://www.dhondt.de/ccx_2.20.src.tar.bz2 -O /src/ccx.tbz2 \
 && mkdir -p /src/CalculiX && tar xjf /src/ccx.tbz2 -C /src/CalculiX --strip-components=2 ./CalculiX/ccx_2.20 \
 && git init -q /opt/fsi/calculix-adapter && cd /opt/fsi/calculix-adapter \
 && git remote add origin https://github.com/precice/calculix-adapter.git \
 && git fetch -q --depth 1 origin "$CCX_ADAPTER_SHA" && git checkout -q FETCH_HEAD \
 && PKG_CONFIG_PATH=/opt/fsi/precice/lib/pkgconfig \
      make -j"$JOBS" CCX=/src/CalculiX/ccx_2.20/src CC=gcc CXX=g++ FC=gfortran \
 && test -x /opt/fsi/calculix-adapter/bin/ccx_preCICE \
 && rm -rf /opt/fsi/calculix-adapter/.git
# 3) openfoam-adapter against ESI OpenFOAM 2512, then lift the .so out of $HOME
RUN git init -q /src/openfoam-adapter && cd /src/openfoam-adapter \
 && git remote add origin https://github.com/precice/openfoam-adapter.git \
 && git fetch -q --depth 1 origin "$OF_ADAPTER_SHA" && git checkout -q FETCH_HEAD
# OpenFOAM's bashrc returns non-zero on benign probes, hence `|| true` on the source alone.
RUN set -e; cd /src/openfoam-adapter \
 ; source /usr/lib/openfoam/openfoam2512/etc/bashrc || true \
 ; WM_NCOMPPROCS="$JOBS" PKG_CONFIG_PATH=/opt/fsi/precice/lib/pkgconfig ./Allwmake \
 ; mkdir -p /opt/fsi/openfoam-adapter \
 ; cp -a "$FOAM_USER_LIBBIN" /opt/fsi/openfoam-adapter/lib \
 ; test -f /opt/fsi/openfoam-adapter/lib/libpreciceAdapterFunctionObject.so

# ---------------------------------------------------------------------------------
# OpenFOAM-7 (.org) + openInjMoldSim. HOME=/opt/of7 puts the whole OF-7 tree under a
# fixed prefix instead of /root; the solver and its one library are then promoted
# from the per-user dirs into OF-7's site dirs, which a job running with a different
# $HOME (GitHub container jobs use /github/home) still finds.
FROM system AS oims
ARG JOBS=4
ARG OF7_SHA=7458f48c2fb109e8dede5d9aec5ecbb904dc1b18
ARG OIMS_TAG=v7.2
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
COPY tools/build_openinjmoldsim.sh /src/ankusdrive/tools/build_openinjmoldsim.sh
RUN mkdir -p /opt/of7 \
 && HOME=/opt/of7 PREFIX=/opt/of7/opt JOBS="$JOBS" OIMS_TAG="$OIMS_TAG" \
      bash /src/ankusdrive/tools/build_openinjmoldsim.sh --build \
 && got="$(git -C /opt/of7/OpenFOAM/OpenFOAM-7 rev-parse HEAD)" \
 && { [ "$got" = "$OF7_SHA" ] || { echo "OpenFOAM-7 version-7 moved: $got != pinned $OF7_SHA" >&2; exit 1; }; }
RUN set -e; export HOME=/opt/of7 \
 ; source /opt/of7/OpenFOAM/OpenFOAM-7/etc/bashrc || true \
 ; user_dir="/opt/of7/OpenFOAM/root-7/platforms/$WM_OPTIONS" \
 ; site_dir="$WM_PROJECT_INST_DIR/site/7/platforms/$WM_OPTIONS" \
 ; test "$site_dir" = /opt/of7/OpenFOAM/site/7/platforms/linux64GccDPInt32Opt \
 ; mkdir -p "$site_dir/bin" "$site_dir/lib" \
 ; cp -a "$user_dir/bin/." "$site_dir/bin/" \
 ; cp -a "$user_dir/lib/." "$site_dir/lib/" \
 ; test -x "$site_dir/bin/openInjMoldSim" \
 ; rm -rf /opt/of7/OpenFOAM/OpenFOAM-7/platforms/*/src /opt/of7/OpenFOAM/OpenFOAM-7/.git \
          /opt/of7/OpenFOAM/ThirdParty-7/build /opt/of7/OpenFOAM/ThirdParty-7/.git \
          /opt/of7/OpenFOAM/root-7 /opt/of7/opt/openInjMoldSim/.git

# ---------------------------------------------------------------------------------
FROM system AS final
COPY --from=yade    /opt/yade          /opt/yade
COPY --from=openems /opt/openEMS       /opt/openEMS
COPY --from=openems /opt/venv-openems  /opt/venv-openems
COPY --from=fsi     /opt/fsi           /opt/fsi
COPY --from=oims    /opt/of7           /opt/of7

# Only what provisioning reads — a broader COPY would rebuild this stage on every
# unrelated commit. CI installs its own checkout over this with --no-deps.
COPY pyproject.toml README.md LICENSE NOTICE /src/ankusdrive/
COPY ankusdrive /src/ankusdrive/ankusdrive
COPY scripts /src/ankusdrive/scripts

# Driver venv: every in-process wheel family + the suite's own tooling. `python3` on
# PATH is this venv, so run_all.sh (with no .venv in the checkout) runs every suite
# under it — the same one-env collapse test.yml uses.
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN python3 -m venv /opt/venv \
 && pip install -q --upgrade pip \
 && pip install -q "/src/ankusdrive[mbd,topology,optics,fluids,optics_gpl]" "ruff==0.15.15" build

# Bempp (dedicated venv — meshio>=5 clashes with solidspy's meshio==3)
RUN BEMPP_VENV=/opt/venv-bempp bash /src/ankusdrive/scripts/install-solvers.sh acoustics_bem

# FreeCAD from the pinned AppImage, live-verified (worker ping + a CalculiX solve)
RUN cd /src/ankusdrive && PY=/opt/venv/bin/python3 PREFIX=/opt/freecad BINDIR=/usr/local/bin \
      bash scripts/install-freecad-appimage.sh

# Absolute wiring. Discovery would find most of these under $HOME, but a GitHub
# container job runs with HOME=/github/home, so nothing may depend on it.
ENV ANKUSDRIVE_FREECADCMD=/opt/freecad/squashfs-root/usr/bin/freecadcmd \
    ANKUSDRIVE_CALCULIX_PATH=/usr/bin/ccx \
    ANKUSDRIVE_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc \
    # is_available("openfoam") — what test_openfoam gates on — needs the BINARY;
    # the apt install puts it on PATH only after the bashrc is sourced
    ANKUSDRIVE_OPENFOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/linux64GccDPInt32Opt/bin/simpleFoam \
    ANKUSDRIVE_FSI_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc \
    ANKUSDRIVE_CCX_PRECICE=/opt/fsi/calculix-adapter/bin/ccx_preCICE \
    ANKUSDRIVE_PRECICE_LIB=/opt/fsi/precice/lib \
    ANKUSDRIVE_OPENFOAM_ADAPTER_LIB=/opt/fsi/openfoam-adapter/lib \
    ANKUSDRIVE_YADE=/opt/yade/bin/yade \
    ANKUSDRIVE_OPENEMS_PYTHON=/opt/venv-openems/bin/python \
    ANKUSDRIVE_BEMPP_PYTHON=/opt/venv-bempp/bin/python \
    ANKUSDRIVE_OPENINJMOLDSIM=/opt/of7/OpenFOAM/site/7/platforms/linux64GccDPInt32Opt/bin/openInjMoldSim \
    ANKUSDRIVE_OPENINJMOLDSIM_BASHRC=/opt/of7/OpenFOAM/OpenFOAM-7/etc/bashrc \
    LD_LIBRARY_PATH=/opt/openEMS/lib

COPY docker/heavy-solvers/LICENSES.md /licenses/README.md

# The image is not done until every heavy solver resolves — the same gate the CI
# job runs before the suite, so a broken build fails here and not as a lane of SKIPs.
RUN cd /src/ankusdrive && bash scripts/ci-linux-preflight.sh \
 && pip uninstall -q -y ankusdrive

LABEL org.opencontainers.image.title="ankusdrive-heavy" \
      org.opencontainers.image.description="AnkusDrive heavy-solver CI image (OpenFOAM, Elmer, YADE, openEMS, preCICE FSI, openInjMoldSim, Bempp, FreeCAD)" \
      org.opencontainers.image.source="https://github.com/gchen19/AnkusDrive" \
      org.opencontainers.image.licenses="See /licenses/README.md (bundles GPL-3.0, LGPL-3.0 and other components)"
WORKDIR /
