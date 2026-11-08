"""Export an independent selection without serializing/replacing the full model."""
import zipfile
import numpy as np
from minicolmap.semantics.project import write_json,sha256


def save_selection(output,state,normalization,indices,metadata,**arrays):
    output.mkdir(parents=True,exist_ok=True)
    scale=normalization['scale']
    world=state['means'][indices].numpy()*scale+np.asarray(normalization['center'])
    np.savez_compressed(output/'selection.npz',indices=indices,**arrays)
    metadata['selection_sha256']=sha256(output/'selection.npz')
    write_json(output/'selection.json',metadata)
    np.savez_compressed(output/'gaussians.npz',indices=indices,means_world=world.astype(np.float32),
        log_scales_world=state['log_scales'][indices].numpy()+np.log(scale),quaternions_wxyz=state['quaternions'][indices].numpy(),
        opacity_logits=state['opacity_logits'][indices].numpy(),sh_coeffs=state['sh_coeffs'][indices].numpy())
    colors=np.clip(.5+.28209479177387814*state['sh_coeffs'][indices,0].numpy(),0,1)
    with (output/'centers.ply').open('w') as file:
        file.write(f'ply\nformat ascii 1.0\ncomment Gaussian centers; DC preview colors; not a mesh\nelement vertex {len(indices)}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
        np.savetxt(file,np.column_stack([world,np.rint(colors*255)]),fmt=['%.7g','%.7g','%.7g','%d','%d','%d'])
    (output/'README.txt').write_text(f"Independent Gaussian selection: {metadata['text']}. Original RGB and language models remain unchanged.\nselection.npz: original Gaussian indices and selection weights. gaussians.npz: world-coordinate Gaussian parameters, quaternions wxyz. centers.ply: center points with DC preview colors, not a mesh.\nSfM reconstruction coordinates have arbitrary scale, not meters. Bounds contain selected Gaussian centers. Visible-surface prototype; occluded surfaces can be incomplete and nearby clutter can be included.\n")
    with zipfile.ZipFile(output/'selection.zip.tmp','w',zipfile.ZIP_DEFLATED) as archive:
        for name in ('selection.json','selection.npz','gaussians.npz','centers.ply','README.txt'):archive.write(output/name,name)
        for name in ('validation.json','policy.json','README.md','teacher-sources.json','visible-view-audit.json'):
            if (output/name).is_file():archive.write(output/name,name)
    (output/'selection.zip.tmp').replace(output/'selection.zip')
