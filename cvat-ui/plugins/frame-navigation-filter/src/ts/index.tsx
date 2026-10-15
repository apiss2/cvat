// SPDX-License-Identifier: MIT
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createNavigationFilterPlugin } from './filter';

const builder: ComponentBuilder = ({ core }) => {
    let enabled = true;
    const plugin = createNavigationFilterPlugin(() => enabled);
    void core.plugins.register(plugin).catch((error) => {
        enabled = false;
        console.error('Could not register frame navigation filter', error);
    });
    return {
        name: plugin.name,
        // Core has no unregister API. Make the installed hook a pass-through.
        destructor: (): void => { enabled = false; },
    };
};

let registered = false;
function register(): void {
    const host = (window as unknown as { cvatUI?: { registerComponent: PluginEntryPoint } }).cvatUI;
    if (host && !registered) { registered = true; host.registerComponent(builder); }
}
window.addEventListener('plugins.ready', register, { once: true });
register();
