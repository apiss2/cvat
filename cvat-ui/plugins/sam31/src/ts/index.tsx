// SPDX-License-Identifier: MIT
import type { PluginEntryPoint } from 'components/plugins-entrypoint';
import { trackingPlugin } from '../../../sam2/src/ts/tracking-dialog';

// UI and the CVAT tracker wire contract are shared; model code, weights and Redis
// state live in the independent SAM3.1 function and never enter CVAT containers.
const builder = trackingPlugin({ name: 'SAM3.1', functionID: 'pth-sam31-tracker' });
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
