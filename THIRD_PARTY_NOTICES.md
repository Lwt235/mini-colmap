# Third-party components and data

Mini-COLMAP integrates the pretrained models, CUDA rasterizers, reference
pipelines and datasets attributed below. Each component and dataset remains
subject to its upstream license and usage terms, independently of the license
for Mini-COLMAP's own code.

## Core and workbench dependencies

- [NumPy](https://numpy.org/), [SciPy](https://scipy.org/) and
  [OpenCV](https://opencv.org/): arrays, numerical solvers, SIFT, matching and PnP.
- [PyTorch](https://pytorch.org/): automatic differentiation and GPU tensors.
- [gsplat](https://github.com/nerfstudio-project/gsplat), pinned to **1.5.3**:
  CUDA Gaussian rasterization. The project's geometry, training policies and
  task orchestration are separate from the library's rasterization kernels.
- [FastAPI](https://fastapi.tiangolo.com/), Starlette and Uvicorn: HTTP,
  WebSocket and local service infrastructure.
- [React](https://react.dev/), [Three.js](https://threejs.org/), Vite and their
  npm dependencies: the browser interface. Installed package license notices
  also apply to the bundled frontend. See the
  [npm lockfile](web/package-lock.json) for exact versions.

## Semantic models and reference implementations

- [Segment Anything](https://github.com/facebookresearch/segment-anything):
  SAM ViT-H region proposals; code and official weights are Apache-2.0.
  The install URL in [semantic requirements](requirements-semantic.txt) pins
  commit `dca509fe793f601edb92606367a655c15ac00fdf`.
- [OpenCLIP](https://github.com/mlfoundations/open_clip): image/text encoders.
  The ViT-B/16 `laion2b_s34b_b88k` model uses Hub revision
  `7288da5a0d6f0b51c4a2b27c624837a9236d0112`; its model card declares MIT.
  Weight download provenance is described in [model assets](models/README.md).
- [LangSplat](https://github.com/minghanqin/LangSplat): reference preprocessing,
  autoencoder, language-field training and evaluation conventions. The recorded
  reference revision is `d70edb86df0fcbda19dc0d9739a3e5140a5e65fc`, with feature
  rasterizer `329f8e8c64a56f3f018397faa4a4996cbf024de0` and SAM submodule
  `e5dbe4b5616e24f02f15ce5a439a5edf228b3a75`.
  Reference source and compiled extensions are downloaded into ignored local
  caches. Check each pinned upstream `LICENSE` before redistribution or
  commercial use.
- [Original 3D Gaussian Splatting](https://github.com/graphdeco-inria/gaussian-splatting):
  reference for parameter grouping, optimizer-state migration, density control
  and L1/D-SSIM loss. Mini-COLMAP implements its own training orchestration using
  these references. Original 3DGS/LangSplat reference components may carry
  research/non-commercial terms, which apply independently to those components.

## Datasets and figures

- [Middlebury multi-view data](https://vision.middlebury.edu/mview/data/):
  Temple/TempleRing and Dino photographs and calibration. Source URLs and
  observed archive hashes are retained by the preparation scripts.
- [LERF](https://www.lerf.io/) / LangSplat LERF-OVS: photographs, COLMAP geometry
  and polygon labels; see the [dataset guide](docs/lerf-data-guide.md).
- Workbench screenshots and validation renders are actual project outputs.
  Their source paths, hashes and measurement scope are recorded in
  [benchmark metadata](docs/assets/benchmarks.json) and the
  [compact evidence index](reports/README.md). Rendered or displayed source
  photographs still inherit their dataset's usage conditions.

Large weights, training checkpoints, photos and downloaded reference source
are excluded from the public source snapshot. This notice records provenance;
permissions for third-party materials are governed by their respective licenses.
