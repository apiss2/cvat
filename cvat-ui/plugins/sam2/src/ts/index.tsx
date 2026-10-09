// SPDX-License-Identifier: MIT
import type { PluginEntryPoint } from 'components/plugins-entrypoint';
import { SAM2_TRACKER } from './action';
import { trackingPlugin } from './tracking-dialog';

const builder = trackingPlugin(SAM2_TRACKER);
let registered = false;
function register(): void {
    const host = (window as unknown as { cvatUI?: { registerComponent: PluginEntryPoint } }).cvatUI;
    if (host && !registered) {
        registered = true;
        host.registerComponent(builder);
    }
}
window.addEventListener('plugins.ready', register, { once: true });
register();
