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
#   elmer   install-solvers.sh elmer       -> /opt/elmer (arm64 only; amd64 uses the PPA)
#   final   system + the five prefixes + FreeCAD AppImage (+ Render add-on, POV-Ray)
#           + Bempp venv + driver venv
#
# Build locally:  docker build -f docker/heavy-solvers/Dockerfile --build-arg JOBS=8 .
#
# Licences of the bundled components: docker/heavy-solvers/LICENSES.md.

ARG UBUNTU=ubuntu:24.04
# The slim solver image (#422) reads these; declared here because a global ARG must
# precede the first FROM. HEAVY_IMAGE is only pulled with SOLVER_SRC=published.
ARG SOLVER_SRC=stages
ARG HEAVY_IMAGE=ghcr.io/gchen19/ankusdrive-heavy:latest

# ---------------------------------------------------------------------------------
FROM ${UBUNTU} AS system
ARG DEBIAN_FRONTEND=noninteractive
# Set by BuildKit per platform (amd64 | arm64). The image is multi-arch (#363).
ARG TARGETARCH
# OpenFOAM's etc/bashrc derives the user dir from $USER, which docker leaves unset.
ENV USER=root LANG=C.UTF-8
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl wget gnupg software-properties-common git bzip2 unzip file \
      python3 python3-venv python3-dev python3-pip \
      build-essential cmake gfortran pkg-config ccache flex bison \
 && curl -fsSL https://dl.openfoam.com/add-debian-repo.sh | bash \
 && if [ "$TARGETARCH" = "amd64" ]; then add-apt-repository -y ppa:elmer-csc-ubuntu/elmer-csc-ppa; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      openfoam2512-dev \
      calculix-ccx prusa-slicer \
      # Elmer: amd64 only (the CSC PPA publishes no arm64 build, noble ships none).
      # arm64 instead pins gcc-11 for OpenFOAM-7's synthesized linuxArm64 rules (#276).
      $([ "$TARGETARCH" = "amd64" ] && echo elmerfem-csc || echo gcc-11 g++-11) \
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
# Solvers this ARCH deliberately lacks, one "<solver><TAB><reason>" per line. The
# preflight reads it (ANKUSDRIVE_HEAVY_EXCLUSIONS): a listed solver is reported as
# excluded instead of failing, an unlisted one must resolve, and a listed one that
# DOES resolve fails as a stale exclusion — so no gap is ever a silent SKIP.
RUN mkdir -p /etc/ankusdrive-heavy \
 && if [ "$TARGETARCH" = "amd64" ]; then : > /etc/ankusdrive-heavy/exclusions; \
    else printf 'elmer\tno %s build: the Elmer CSC PPA publishes amd64 only and Ubuntu noble ships no elmer package (#363)\n' "$TARGETARCH" > /etc/ankusdrive-heavy/exclusions; fi
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
# OF7_ARM64_CPU_FLAG: never -mcpu=native in a published image — it would bake in the
# build runner's CPU and SIGILL elsewhere (an Apple-Silicon Docker host, another cloud).
RUN mkdir -p /opt/of7 \
 && HOME=/opt/of7 PREFIX=/opt/of7/opt JOBS="$JOBS" OIMS_TAG="$OIMS_TAG" \
    OF7_ARM64_CPU_FLAG=-march=armv8-a \
      bash /src/ankusdrive/tools/build_openinjmoldsim.sh --build \
 && got="$(git -C /opt/of7/OpenFOAM/OpenFOAM-7 rev-parse HEAD)" \
 && { [ "$got" = "$OF7_SHA" ] || { echo "OpenFOAM-7 version-7 moved: $got != pinned $OF7_SHA" >&2; exit 1; }; }
RUN set -e; export HOME=/opt/of7 \
 ; source /opt/of7/OpenFOAM/OpenFOAM-7/etc/bashrc || true \
 ; user_dir="/opt/of7/OpenFOAM/root-7/platforms/$WM_OPTIONS" \
 ; site_dir="$WM_PROJECT_INST_DIR/site/7/platforms/$WM_OPTIONS" \
 ; case "$site_dir" in /opt/of7/OpenFOAM/site/7/platforms/*Opt) ;; *) echo "unexpected site dir $site_dir" >&2; exit 1 ;; esac \
 ; mkdir -p "$site_dir/bin" "$site_dir/lib" \
 ; ln -sfn "$WM_OPTIONS" /opt/of7/OpenFOAM/site/7/platforms/current \
 ; cp -a "$user_dir/bin/." "$site_dir/bin/" \
 ; cp -a "$user_dir/lib/." "$site_dir/lib/" \
 ; test -x "$site_dir/bin/openInjMoldSim" \
 ; rm -rf /opt/of7/OpenFOAM/OpenFOAM-7/platforms/*/src /opt/of7/OpenFOAM/OpenFOAM-7/.git \
          /opt/of7/OpenFOAM/ThirdParty-7/build /opt/of7/OpenFOAM/ThirdParty-7/.git \
          /opt/of7/OpenFOAM/root-7 /opt/of7/opt/openInjMoldSim/.git

# ---------------------------------------------------------------------------------
# Elmer from source (#384). The CSC PPA the system stage uses publishes amd64 only and
# noble ships no elmer package, so arm64 builds a pinned release; amd64 keeps the PPA
# binaries and this stage leaves an empty prefix. /opt/elmer/bin is on discovery's
# Linux search path, so no ANKUSDRIVE_ELMER_PATH (which would differ per arch).
FROM system AS elmer
ARG JOBS=4
ARG TARGETARCH
COPY scripts/install-solvers.sh /src/ankusdrive/scripts/install-solvers.sh
RUN if [ "$TARGETARCH" = "amd64" ]; then mkdir -p /opt/elmer; \
    else ELMER_PREFIX=/opt/elmer ELMER_SRC=/src/elmerfem ELMER_JOBS="$JOBS" \
           bash /src/ankusdrive/scripts/install-solvers.sh elmer \
      && rm -rf /src/elmerfem; fi

# ---------------------------------------------------------------------------------
# Bempp's dedicated venv (meshio>=5 clashes with solidspy's meshio==3, so it is never
# the driver venv). Its own stage since #422: the slim solver image copies this prefix
# too, and a stage is the only thing a COPY --from can name.
FROM system AS bempp
ARG TARGETARCH
COPY scripts/install-solvers.sh /src/ankusdrive/scripts/install-solvers.sh
# bempp's shape generators shell out to a gmsh CLI, which the pip wheel supplies —
# except on Linux aarch64, where PyPI has no gmsh wheel, so arm64 takes it from apt (#363).
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      apt-get update && apt-get install -y --no-install-recommends gmsh && gmsh --version \
      && rm -rf /var/lib/apt/lists/*; \
    fi
RUN BEMPP_VENV=/opt/venv-bempp bash /src/ankusdrive/scripts/install-solvers.sh acoustics_bem

# ---------------------------------------------------------------------------------
FROM system AS final
COPY --from=yade    /opt/yade          /opt/yade
COPY --from=openems /opt/openEMS       /opt/openEMS
COPY --from=openems /opt/venv-openems  /opt/venv-openems
COPY --from=fsi     /opt/fsi           /opt/fsi
COPY --from=oims    /opt/of7           /opt/of7
COPY --from=elmer   /opt/elmer         /opt/elmer
# arm64 now has Elmer, so the system stage's exclusion for it is lifted here rather
# than there (editing the system stage would rebuild every stage on both arches).
# The preflight fails a stale exclusion, so leaving it would fail the build.
RUN sed -i '/^elmer\t/d' /etc/ankusdrive-heavy/exclusions

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

# Bempp comes from its own stage (#422). The gmsh CLI its shape generators shell out
# to is a pip wheel everywhere except Linux aarch64, where arm64 takes it from apt —
# that install lives in the bempp stage, so it must be repeated here for the arm64
# RUNTIME (a venv copy carries no apt package).
ARG TARGETARCH
COPY --from=bempp /opt/venv-bempp /opt/venv-bempp
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      apt-get update && apt-get install -y --no-install-recommends gmsh && gmsh --version; \
    fi

# FreeCAD from the pinned AppImage, live-verified (worker ping + a CalculiX solve)
RUN cd /src/ankusdrive && PY=/opt/venv/bin/python3 PREFIX=/opt/freecad BINDIR=/usr/local/bin \
      bash scripts/install-freecad-appimage.sh

# export_drawing's PDF leg rasterises inside FreeCAD's BUNDLED Python, so svglib +
# reportlab go there, not in the driver venv. svglib<1.6 per pyproject.toml's note
# (1.6 drags in rlPyCairo -> a native pycairo build). Without them test_worker's PDF
# leg SKIPs — the one thing the self-hosted box covered that this image did not (#340).
RUN /opt/freecad/squashfs-root/usr/bin/python -m pip install -q "svglib<1.6" reportlab \
 && /opt/freecad/squashfs-root/usr/bin/python -c "import svglib, reportlab"

# Full Blender for render_photoreal's studio backend (#335). Without it the 7 live
# test_blender_* photoreal tests SKIP here while the self-hosted box PASSes them, the
# parity gap scripts/ci_parity_diff.py found (#342). Discovery takes the /usr/local/bin
# symlink, so no ANKUSDRIVE_BLENDER_PATH: on arm64 it would name a missing file.
# Blender publishes Linux builds for x86_64 only, so arm64 records an exclusion.
# The X libs are what the tarball links beyond the FreeCAD runtime (checked with ldd).
RUN if [ "$TARGETARCH" = "amd64" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        xz-utils libxrender1 libxfixes3 libxi6 libsm6 libice6 libxxf86vm1 \
      && rm -rf /var/lib/apt/lists/* \
      && CACHE_DIR=/tmp/blender-cache BINDIR=/usr/local/bin \
         bash /src/ankusdrive/scripts/install-renderers.sh blender \
      && rm -rf /tmp/blender-cache; \
    else \
      printf 'blender\tno %s build: Blender publishes Linux binaries for x86_64 only (#342)\n' "$TARGETARCH" >> /etc/ankusdrive-heavy/exclusions; \
    fi

# The FreeCAD Render workbench + POV-Ray, so the add-on half of test_render_photoreal.py
# runs instead of SKIPping on every lane (#421). One renderer proves the add-on import,
# scene export, material cards and the render_job API; the other five add-on renderers
# are hand-fetched builds of hundreds of MB and stay SKIPs here. The add-on goes in the
# FreeCAD install's own Mod/ (loaded like a bundled workbench), not the user data dir,
# which follows $HOME. POV-Ray is packaged for both arches; povray-includes is only a
# Recommends, and without it every add-on scene dies parsing `#include "colors.inc"`.
RUN apt-get update && apt-get install -y --no-install-recommends povray povray-includes \
 && rm -rf /var/lib/apt/lists/* \
 && RENDER_ADDON_DIR=/opt/freecad/squashfs-root/usr/Mod/Render \
      bash /src/ankusdrive/scripts/install-renderers.sh render-addon

# Arch-neutral path to the ESI build (linux64… on amd64, linuxARM64… on arm64), so the
# ANKUSDRIVE_* values below — which lanes and users copy out of the image — are the
# same on every platform.
RUN set -e; plat=$(ls -d /usr/lib/openfoam/openfoam2512/platforms/*Opt | head -1) \
 ; test -x "$plat/bin/simpleFoam" \
 ; ln -sfn "$(basename "$plat")" /usr/lib/openfoam/openfoam2512/platforms/current \
 ; test -x /opt/of7/OpenFOAM/site/7/platforms/current/bin/openInjMoldSim

# Absolute wiring. Discovery would find most of these under $HOME, but a GitHub
# container job runs with HOME=/github/home, so nothing may depend on it.
ENV ANKUSDRIVE_FREECADCMD=/opt/freecad/squashfs-root/usr/bin/freecadcmd \
    ANKUSDRIVE_CALCULIX_PATH=/usr/bin/ccx \
    ANKUSDRIVE_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc \
    # is_available("openfoam") — what test_openfoam gates on — needs the BINARY;
    # the apt install puts it on PATH only after the bashrc is sourced
    ANKUSDRIVE_OPENFOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/current/bin/simpleFoam \
    ANKUSDRIVE_FSI_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc \
    ANKUSDRIVE_CCX_PRECICE=/opt/fsi/calculix-adapter/bin/ccx_preCICE \
    ANKUSDRIVE_PRECICE_LIB=/opt/fsi/precice/lib \
    ANKUSDRIVE_OPENFOAM_ADAPTER_LIB=/opt/fsi/openfoam-adapter/lib \
    ANKUSDRIVE_YADE=/opt/yade/bin/yade \
    ANKUSDRIVE_OPENEMS_PYTHON=/opt/venv-openems/bin/python \
    ANKUSDRIVE_BEMPP_PYTHON=/opt/venv-bempp/bin/python \
    ANKUSDRIVE_OPENINJMOLDSIM=/opt/of7/OpenFOAM/site/7/platforms/current/bin/openInjMoldSim \
    ANKUSDRIVE_OPENINJMOLDSIM_BASHRC=/opt/of7/OpenFOAM/OpenFOAM-7/etc/bashrc \
    LD_LIBRARY_PATH=/opt/openEMS/lib \
    ANKUSDRIVE_HEAVY_EXCLUSIONS=/etc/ankusdrive-heavy/exclusions

COPY docker/heavy-solvers/LICENSES.md /licenses/README.md

# The image is not done until every heavy solver resolves — the same gate the CI
# job runs before the suite, so a broken build fails here and not as a lane of SKIPs.
RUN cd /src/ankusdrive && bash scripts/ci-linux-preflight.sh \
 && pip uninstall -q -y ankusdrive

LABEL org.opencontainers.image.title="ankusdrive-heavy" \
      org.opencontainers.image.description="AnkusDrive heavy-solver CI image, amd64 + arm64 (OpenFOAM, Elmer, Blender on amd64 only, FreeCAD Render workbench + POV-Ray, YADE, openEMS, preCICE FSI, openInjMoldSim, Bempp, FreeCAD)" \
      org.opencontainers.image.source="https://github.com/gchen19/AnkusDrive" \
      org.opencontainers.image.licenses="See /licenses/README.md (bundles GPL-3.0, LGPL-3.0 and other components)"
WORKDIR /

# =================================================================================
# The SLIM solver image, `ankusdrive-solvers` (#422) — what a user actually runs under
# ANKUSDRIVE_SUBSTRATE=container. Everything above builds the CI image, which carries
# FreeCAD, the driver venv and every -dev package because the whole suite runs INSIDE
# it. Under the container substrate none of that is used: AnkusDrive and FreeCAD stay
# on the host and only solvers cross `<engine> exec`. Measured on the arm64 image,
# those host-side pieces are ~2.5 GB of a ~4.5 GB download.
#
# So this half installs the RUNTIME packages only (openfoam2512, not -dev; no
# toolchain) and copies the already-built solver prefixes onto it.
#
# WHERE THE PREFIXES COME FROM — `SOLVER_SRC`:
#   stages     (default) the yade/openems/fsi/oims/elmer/bempp stages above, so CI
#              builds it from the same recipe and reuses their layer caches.
#   published  the published heavy image (`HEAVY_IMAGE`), for a local build in
#              minutes instead of the hours those source stages take.
# BuildKit only builds the stages a target needs, so `stages` never pulls the
# published image and `published` never compiles anything.
#
#   docker build -f docker/heavy-solvers/Dockerfile --target slim -t ankusdrive-solvers .
#   docker build -f docker/heavy-solvers/Dockerfile --target slim \
#     --build-arg SOLVER_SRC=published --build-arg HEAVY_IMAGE=ghcr.io/…@sha256:… .
#
# NOT here, by design: FreeCAD, the driver venv, CalculiX (`ccx`), PrusaSlicer,
# Blender, SU2 — every one of those resolves on the HOST under this substrate.

FROM ${HEAVY_IMAGE} AS payloads-published
FROM yade    AS src-yade-stages
FROM openems AS src-openems-stages
FROM fsi     AS src-fsi-stages
FROM oims    AS src-oims-stages
FROM elmer   AS src-elmer-stages
FROM bempp   AS src-bempp-stages
FROM payloads-published AS src-yade-published
FROM payloads-published AS src-openems-published
FROM payloads-published AS src-fsi-published
FROM payloads-published AS src-oims-published
FROM payloads-published AS src-elmer-published
FROM payloads-published AS src-bempp-published
FROM src-yade-${SOLVER_SRC}    AS src-yade
FROM src-openems-${SOLVER_SRC} AS src-openems
FROM src-fsi-${SOLVER_SRC}     AS src-fsi
FROM src-oims-${SOLVER_SRC}    AS src-oims
FROM src-elmer-${SOLVER_SRC}   AS src-elmer
FROM src-bempp-${SOLVER_SRC}   AS src-bempp

# ---------------------------------------------------------------------------------
# Runtime base: the libraries the solvers LINK, with no toolchain and no headers.
#
# The library list is DERIVED, not hand-written: tools/container_runtime_deps.sh ldd's
# every ELF in the solver prefixes and maps each dependency to its apt package. It is
# derived on ONE architecture, so a package the other one names differently — or does
# not have at all, like x86's libquadmath0 — is marked per-arch below. The --check in
# the `slim` stage is what catches the other direction (a library arm64 needs and
# amd64 did not), by failing that architecture's build and naming it. To regenerate
# after a solver pin moves:
#   docker run --rm -v "$PWD/tools:/t:ro" \
#     -e EXTRA_LIB_DIRS=/usr/lib/openfoam/openfoam2512/platforms/current/lib \
#     <heavy image> bash /t/container_runtime_deps.sh /opt/yade /opt/fsi /opt/of7 \
#       /opt/openEMS /opt/venv-openems /opt/venv-bempp
# The `slim` stage then re-runs that tool with --check, so a list that has fallen
# short fails THIS build, on THIS architecture, instead of a user's solve.
FROM ${UBUNTU} AS runtime
ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
# OpenFOAM's etc/bashrc derives the user dir from $USER, which docker leaves unset.
ENV USER=root LANG=C.UTF-8
# BuildKit always sets TARGETARCH; a builder that does not would silently skip the
# per-arch packages below (amd64's Elmer, arm64's BLAS/gmsh) and publish an image
# missing a solver. Fail here instead.
RUN case "$TARGETARCH" in amd64|arm64) ;; \
    *) echo "TARGETARCH=${TARGETARCH:-<unset>} — build with BuildKit, or pass --build-arg TARGETARCH=amd64|arm64" >&2; exit 1 ;; esac
# `curl | bash` for the ESI repo is what scripts/install-solvers.sh documents and what
# the system stage above does; pinning that keyring is tracked in #423.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
      software-properties-common \
 && curl -fsSL https://dl.openfoam.com/add-debian-repo.sh | bash \
 && if [ "$TARGETARCH" = "amd64" ]; then add-apt-repository -y ppa:elmer-csc-ubuntu/elmer-csc-ppa; fi \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      openfoam2512 \
      # YADE embeds the SYSTEM python, so part of what it needs are IMPORTS, which ldd
      # cannot see and the derived list below therefore misses: numpy and mpmath at
      # start-up, IPython as soon as `yade` runs a script (yade.runtime imports it).
      # The check in the slim stage RUNS a script for exactly this reason.
      python3 python3-numpy python3-mpmath ipython3 \
      $([ "$TARGETARCH" = "amd64" ] && echo elmerfem-csc) \
      $([ "$TARGETARCH" = "arm64" ] && echo gmsh) \
      libaec0 libamd3 libarpack2t64 libboost-atomic1.83.0 libboost-chrono1.83.0t64 \
      libboost-date-time1.83.0 libboost-filesystem1.83.0 libboost-iostreams1.83.0 \
      libboost-log1.83.0 libboost-program-options1.83.0 libboost-python1.83.0 \
      libboost-regex1.83.0 libboost-serialization1.83.0 libboost-system1.83.0 \
      libboost-thread1.83.0 libbrotli1 libbsd0 libbz2-1.0 libcamd3 libcap2 libccolamd3 \
      libcholmod5 libcolamd3 libcurl4t64 libdeflate0 libdouble-conversion3 \
      libevent-core-2.1-7t64 libevent-pthreads-2.1-7t64 libexpat1 libffi8 \
      libfontconfig1 libfreetype6 libgfortran5 libgl1 libgl2ps1.4 libglib2.0-0t64 \
      libglu1-mesa libglvnd0 libglx0 libgmp10 libgmpxx4ldbl libgomp1 libgts-0.7-5t64 \
      libhdf5-103-1t64 libhdf5-hl-100t64 libhwloc15 libicu74 libjbig0 libjpeg-turbo8 \
      libjsoncpp25 liblerc4 liblz4-1 liblzma5 libmd0 libopenblas0-pthread libopengl0 \
      libopenmpi3t64 libpcre2-8-0 libpng16-16t64 libpython3.12t64 \
      libreadline8t64 libsharpyuv0 libspooles2.2t64 libssl3t64 libsuitesparseconfig7 \
      libsz2 libtbb12 libtiff6 libtinfo6 libtinyxml2.6.2v5 libudev1 libvtk9.1t64 \
      libwebp7 libx11-6 libxau6 libxcb1 libxcursor1 libxdmcp6 libxext6 libxfixes3 \
      libxft2 libxinerama1 libxml2 libxrender1 libzstd1 zlib1g \
      # libquadmath is GCC's x86 __float128 runtime — no such package on arm64.
      # arm64 instead needs BLAS/LAPACK: its Elmer is the source build (WITH_MPI=OFF,
      # libblas/liblapack), where amd64 takes the PPA package and its dependencies.
      $([ "$TARGETARCH" = "amd64" ] && echo libquadmath0) \
      $([ "$TARGETARCH" = "arm64" ] && echo libblas3 liblapack3) \
 && apt-get purge -y --auto-remove software-properties-common gnupg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /

# ---------------------------------------------------------------------------------
# Assemble the solver tree, then TRIM it — in this throwaway stage, because deleting
# a file in a later layer does not reclaim its bytes: the data still ships in the
# layer that added it. `slim` below copies the finished /opt in ONE layer, so what is
# removed here is genuinely absent from the published image.
FROM runtime AS assemble
ARG TARGETARCH
COPY --from=src-yade    /opt/yade         /opt/yade
COPY --from=src-openems /opt/openEMS      /opt/openEMS
COPY --from=src-openems /opt/venv-openems /opt/venv-openems
COPY --from=src-fsi     /opt/fsi          /opt/fsi
COPY --from=src-oims    /opt/of7          /opt/of7
COPY --from=src-elmer   /opt/elmer        /opt/elmer
COPY --from=src-bempp   /opt/venv-bempp   /opt/venv-bempp

# One Elmer path on both architectures: amd64 takes the PPA binaries in /usr/bin,
# arm64 the source build already under /opt/elmer/bin. Discovery probes either, but a
# single published ANKUSDRIVE_ELMER_PATH is what a user copies out of the image — and
# sibling_bin resolves ElmerGrid/ViewFactors NEXT TO it, so all three are linked.
RUN if [ "$TARGETARCH" = "amd64" ]; then \
      mkdir -p /opt/elmer/bin \
      && for b in ElmerSolver ElmerGrid ViewFactors; do ln -sfn "/usr/bin/$b" "/opt/elmer/bin/$b"; done; \
    fi \
 && for b in ElmerSolver ElmerGrid ViewFactors; do test -x "/opt/elmer/bin/$b" || { echo "missing /opt/elmer/bin/$b" >&2; exit 1; }; done

# OpenFOAM-7 ships its build tree: 688 .o/.dep intermediates under platforms/*/
# applications (363 MB), plus the source, tutorials and docs a runtime image never
# reads — openInjMoldSim needs only bin/, lib/, etc/ (the bashrc sources config.sh)
# and bin/foamEtcFile. ~560 MB.
RUN rm -rf /opt/of7/OpenFOAM/OpenFOAM-7/platforms/*/applications \
           /opt/of7/OpenFOAM/OpenFOAM-7/tutorials \
           /opt/of7/OpenFOAM/OpenFOAM-7/src \
           /opt/of7/OpenFOAM/OpenFOAM-7/applications \
           /opt/of7/OpenFOAM/OpenFOAM-7/doc \
           /opt/of7/OpenFOAM/OpenFOAM-7/test

# ---------------------------------------------------------------------------------
# The published image: the runtime base plus the assembled solver tree, one layer.
FROM runtime AS slim
COPY --from=assemble /opt /opt

# Arch-neutral path to the ESI build, so the published ANKUSDRIVE_* values are the
# same on every platform (the heavy image does this too).
RUN set -e; plat=$(ls -d /usr/lib/openfoam/openfoam2512/platforms/*Opt | head -1) \
 ; test -x "$plat/bin/simpleFoam" \
 ; ln -sfn "$(basename "$plat")" /usr/lib/openfoam/openfoam2512/platforms/current \
 ; test -x /opt/of7/OpenFOAM/site/7/platforms/current/bin/openInjMoldSim

ENV ANKUSDRIVE_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc \
    ANKUSDRIVE_OPENFOAM_PATH=/usr/lib/openfoam/openfoam2512/platforms/current/bin/simpleFoam \
    ANKUSDRIVE_FSI_OPENFOAM_BASHRC=/usr/lib/openfoam/openfoam2512/etc/bashrc \
    ANKUSDRIVE_CCX_PRECICE=/opt/fsi/calculix-adapter/bin/ccx_preCICE \
    ANKUSDRIVE_PRECICE_LIB=/opt/fsi/precice/lib \
    ANKUSDRIVE_OPENFOAM_ADAPTER_LIB=/opt/fsi/openfoam-adapter/lib \
    ANKUSDRIVE_YADE=/opt/yade/bin/yade \
    ANKUSDRIVE_ELMER_PATH=/opt/elmer/bin/ElmerSolver \
    ANKUSDRIVE_OPENEMS_PYTHON=/opt/venv-openems/bin/python \
    ANKUSDRIVE_BEMPP_PYTHON=/opt/venv-bempp/bin/python \
    ANKUSDRIVE_OPENINJMOLDSIM=/opt/of7/OpenFOAM/site/7/platforms/current/bin/openInjMoldSim \
    ANKUSDRIVE_OPENINJMOLDSIM_BASHRC=/opt/of7/OpenFOAM/OpenFOAM-7/etc/bashrc \
    LD_LIBRARY_PATH=/opt/openEMS/lib

COPY docker/heavy-solvers/LICENSES.md /licenses/README.md

# The image is not done until (a) every solver's DT_NEEDED resolves — the trimmed
# package list is complete on THIS architecture — and (b) each solver actually runs.
# A missing runtime library otherwise surfaces as a failed solve on a user's Mac.
COPY tools/container_runtime_deps.sh /usr/local/bin/ankusdrive-runtime-deps
RUN EXTRA_LIB_DIRS=/usr/lib/openfoam/openfoam2512/platforms/current/lib \
      bash /usr/local/bin/ankusdrive-runtime-deps --check \
        /opt/yade /opt/fsi /opt/of7 /opt/openEMS /opt/venv-openems /opt/venv-bempp
# Each solver must RUN, not merely exist. Checked with the command's own exit status
# and a banner match on stdout — `… 2>&1 | grep -qi yade` would pass on a traceback
# that merely names the path, which is how a missing python3-mpmath first slipped by.
RUN set -e \
 ; /opt/yade/bin/yade --version > /tmp/yade.txt 2>&1 \
 ; grep -qi "yade version" /tmp/yade.txt \
   # …and RUN a script the way the worker does: `--version` never imports
   # yade.runtime, so it passes on an image where every real DEM solve dies on a
   # missing IPython. `pack`/`utils` are what dem_gpl_runner.py imports.
 ; printf 'from yade import pack, utils\nprint("yade-script-ok")\n' > /tmp/yade_probe.py \
 ; /opt/yade/bin/yade -x -n /tmp/yade_probe.py 2>&1 | grep -q yade-script-ok \
 ; /opt/elmer/bin/ElmerSolver -v > /tmp/elmer.txt 2>&1 \
 ; grep -q "ELMER SOLVER" /tmp/elmer.txt \
 ; /opt/venv-openems/bin/python -c "import openEMS, CSXCAD" \
 ; /opt/venv-bempp/bin/python -c "import bempp_cl" \
 ; test -x "$ANKUSDRIVE_CCX_PRECICE" && test -d "$ANKUSDRIVE_PRECICE_LIB" \
 ; test -f "$ANKUSDRIVE_OPENFOAM_ADAPTER_LIB/libpreciceAdapterFunctionObject.so" \
 ; bash -c 'source "$ANKUSDRIVE_OPENFOAM_BASHRC" >/dev/null 2>&1; simpleFoam -help >/dev/null' \
 ; bash -c 'unset FOAM_SIGFPE; source "$ANKUSDRIVE_OPENINJMOLDSIM_BASHRC" >/dev/null 2>&1; openInjMoldSim -help >/dev/null 2>&1 || true'

LABEL org.opencontainers.image.title="ankusdrive-solvers" \
      org.opencontainers.image.description="AnkusDrive slim solver image for ANKUSDRIVE_SUBSTRATE=container, amd64 + arm64 (OpenFOAM, Elmer, YADE, openEMS, preCICE FSI, openInjMoldSim, Bempp) — runtime only: no FreeCAD, no toolchain" \
      org.opencontainers.image.source="https://github.com/gchen19/AnkusDrive" \
      org.opencontainers.image.licenses="See /licenses/README.md (bundles GPL-3.0, LGPL-3.0 and other components)"
