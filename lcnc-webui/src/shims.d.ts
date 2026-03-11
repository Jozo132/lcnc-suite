declare module 'troika-three-text' {
  import { Object3D } from 'three';
  export class Text extends Object3D {
    text: string;
    fontSize: number;
    color: number | string;
    anchorX: string | number;
    anchorY: string | number;
    font: string | null;
    outlineWidth: number | string;
    outlineColor: number | string;
    depthWrite: boolean;
    sync(callback?: () => void): void;
    dispose(): void;
  }
}
