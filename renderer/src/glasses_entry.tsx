import React from 'react';
import { createRoot } from 'react-dom/client';
import { installWebCc } from '../web_cc';
import { GlassesController, type GlassesCc } from './glasses_controller';
import { GlassesApp } from './glasses_app';

installWebCc();
const controller = new GlassesController((window as unknown as { cc: GlassesCc }).cc, undefined, (window as any).__PENTACLE_CONFIG__);
const root = createRoot(document.getElementById('glasses-root')!);
root.render(<GlassesApp controller={controller}/>);
void controller.start();
window.addEventListener('pagehide', () => { controller.dispose(); root.unmount(); }, { once: true });
