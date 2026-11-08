# Semantic model assets

Large weights are local and ignored by Git. `manifest.json` records size, SHA256,
upstream version and license. Run `scripts/setup_semantic_models.py` to reproduce
the download, then `mini-colmap semantic doctor --config configs/semantic_waldo.json`.

- SAM ViT-H: Meta's official `sam_vit_h_4b8939.pth`, Apache-2.0.
- OpenCLIP ViT-B/16 `laion2b_s34b_b88k`: LAION's safetensors, fixed Hub revision
  `7288da5a0d6f0b51c4a2b27c624837a9236d0112`, model card license MIT.

The CLIP tokenizer is OpenCLIP's bundled BPE tokenizer, so inference loads local
weights without contacting the Hub. Neither these models nor the existing LERF
photos need to be downloaded again after setup.
