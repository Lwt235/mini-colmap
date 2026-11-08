"""CPU-only paired postprocessing control on exactly the same 2D relevancy maps."""
from pathlib import Path
import json
import numpy as np
import torch
import open_clip
from minicolmap.semantics.algorithms import build_autoencoder,text_relevancy
from minicolmap.semantics.compression import build_codec
from minicolmap.semantics.project import write_json
from langsplat_reference_metrics import evaluate_maps,load_metrics


def main():
    torch.set_num_threads(4)
    root = Path(__file__).resolve().parents[1]
    upstream = root/'.cache/langsplat-official'
    workspace = root/'data/semantic_waldo'
    prepared = json.loads((workspace/'project.json').read_text())
    manifest = json.loads((root/'models/manifest.json').read_text())
    clip,_,_ = open_clip.create_model_and_transforms('ViT-B-16',pretrained=str(root/manifest['clip']['path']),device='cpu')
    clip.eval()
    tokenizer = open_clip.get_tokenizer('ViT-B-16')
    old = build_autoencoder(512,3)
    old.load_state_dict(torch.load(workspace/'autoencoder/checkpoint.pt',map_location='cpu',weights_only=True)['state'])
    old.eval()
    pca = build_codec(512,224,codec='pca')
    pca.load_state_dict(torch.load(root/'data/semantic_waldo_repaired/autoencoder/checkpoint.pt',map_location='cpu',weights_only=True)['state'])
    cases = {'raw':lambda x:x,'original-mlp3':old,'repaired-pca224':pca}
    directory = root/'reports/langsplat-reference-20261021/paired-protocol-2d'
    directory.mkdir(parents=True,exist_ok=True)
    source = load_metrics(upstream)
    rows = []
    with torch.inference_mode():
        for annotation in prepared['annotations']:
            image_id = annotation['image_id']
            if image_id not in prepared['splits']['dev']:
                continue
            names = sorted({r['category'] for r in json.loads(Path(annotation['annotation']).read_text())['objects']})
            embeddings = torch.nn.functional.normalize(clip.encode_text(tokenizer([*names,'object','things','stuff','texture'])).float(),dim=-1)
            with np.load(workspace/'regions'/f'{image_id:06d}.npz') as data:
                labels = data['labels']
                features = torch.from_numpy(data['features'])
            for name,model in cases.items():
                predicted = model(features)
                maps = []
                for i,text in enumerate(names):
                    values = text_relevancy(predicted,embeddings[i],embeddings[-4:],temperature=10.).numpy()
                    heat = np.zeros(labels.shape,np.float32)
                    heat[labels>=0] = values[labels[labels>=0]]
                    maps.append(heat)
                maps = np.stack(maps)[None]
                result,_ = evaluate_maps(upstream,maps,names,annotation['annotation'],source=source)
                row = dict(image_id=image_id,variant=name,**result)
                rows.append(row)
                np.savez_compressed(directory/f'{image_id:06d}-{name}.npz',maps=maps,names=np.array(names))
                write_json(directory/f'{image_id:06d}-{name}.json',row)
                print(json.dumps({k:v for k,v in row.items() if k!='per_query'}),flush=True)
    summary = {}
    for name in cases:
        matched = [r for r in rows if r['variant']==name]
        n = sum(len(r['per_query']) for r in matched)
        summary[name] = {protocol:{metric:sum(r[protocol][metric]*len(r['per_query']) for r in matched)/n
                                  for metric in ('mean_iou',('bbox_localization_rate' if protocol=='official' else 'polygon_peak_rate'))}
                         for protocol in ('common','official')}
    write_json(directory/'summary.json',dict(scope='same 2D score maps; dev only; different postprocessing/localization/GT rounding protocols',results=summary))


if __name__=='__main__':
    main()
