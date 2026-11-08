"""CPU-only: replace the AE bundle while holding old 2D regions/features fixed."""
import json
from pathlib import Path
import numpy as np
import torch
import open_clip
import langsplat_reference as reference
from langsplat_reference_metrics import evaluate_maps,load_metrics
from minicolmap.semantics.project import write_json,load_config,project
from minicolmap.semantics.algorithms import text_relevancy


def main():
    root = reference.ROOT
    original = root/'data/semantic_waldo'
    reference.WORK = root/'data/langsplat_ae_old_data_control'
    reference.REPORT = root/'reports/langsplat-reference-20261021/ae-old-data-control'
    reference.REPORT.mkdir(parents=True,exist_ok=True)
    directory = reference.WORK/'language_features'
    directory.mkdir(parents=True,exist_ok=True)
    config = load_config(root/'configs/semantic_waldo.json')
    prepared = project(config)
    frames = {f['image_id']:f for f in prepared['frames']}
    for image_id in prepared['splits']['train']:
        with np.load(original/'regions'/f'{image_id:06d}.npz') as data:
            used = np.unique(data['labels'][data['labels']>=0])
            np.save(directory/f"{Path(frames[image_id]['name']).stem}_f.npy",data['features'][used])
    torch.set_num_threads(4)
    torch.manual_seed(0)
    reference.autoencoder(config,prepared,device='cpu')
    model = reference.upstream_ae('cpu')
    model.load_state_dict(torch.load(reference.WORK/'autoencoder/best_ckpt.pth',map_location='cpu',weights_only=True))
    model.eval()
    manifest = json.loads((root/'models/manifest.json').read_text())
    clip,_,_ = open_clip.create_model_and_transforms('ViT-B-16',pretrained=str(root/manifest['clip']['path']),device='cpu')
    clip.eval()
    tokenizer = open_clip.get_tokenizer('ViT-B-16')
    source = load_metrics(reference.UP)
    rows = []
    with torch.inference_mode():
        for annotation in prepared['annotations']:
            i = annotation['image_id']
            if i not in prepared['splits']['dev']:
                continue
            names = sorted({o['category'] for o in json.loads(Path(annotation['annotation']).read_text())['objects']})
            embeddings = torch.nn.functional.normalize(clip.encode_text(tokenizer([*names,'object','things','stuff','texture'])).float(),dim=-1)
            with np.load(original/'regions'/f'{i:06d}.npz') as data:
                labels = data['labels']
                features = model(torch.from_numpy(data['features']))
            maps = []
            for j,name in enumerate(names):
                scores = text_relevancy(features,embeddings[j],embeddings[-4:],temperature=10.).numpy()
                heat = np.zeros(labels.shape,np.float32)
                heat[labels>=0] = scores[labels[labels>=0]]
                maps.append(heat)
            maps = np.stack(maps)[None]
            metrics,_ = evaluate_maps(reference.UP,maps,names,annotation['annotation'],source=source)
            rows.append(dict(image_id=i,**metrics))
    n = sum(len(r['per_query']) for r in rows)
    summary = {protocol:{metric:sum(r[protocol][metric]*len(r['per_query']) for r in rows)/n
                         for metric in ('mean_iou',('bbox_localization_rate' if protocol=='official' else 'polygon_peak_rate'))}
               for protocol in ('common','official')}
    write_json(reference.REPORT/'summary.json',dict(results=summary,per_view=rows,
               scope='Same old 29357 train regions and old dev maps; only AE architecture/objective/training bundle replaced. CPU seed0; not a single-factor BN ablation.'))
    print(json.dumps(summary,indent=2))


if __name__=='__main__':
    main()
