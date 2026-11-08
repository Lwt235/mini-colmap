import type { ObjectSelection } from './objectSelection';
export type SceneJob = {
  id: string;
  scene_id: string;
  action: 'train' | 'query';
  status: string;
  stage: string;
  message: string;
  progress?: { step: number; total: number };
  result?: {
    image_id: number;
    text: string;
    overlay_url: string;
    selection?: ObjectSelection;
    status: string;
    message: string;
  };
};
export type SearchScene = {
  id: string;
  name: string;
  rgb_run_id: string;
  ready: boolean;
  anchor_image_id: number;
  preview_url: string;
  job?: SceneJob;
};
