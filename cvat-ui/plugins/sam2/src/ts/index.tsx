// SPDX-License-Identifier: MIT
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { SAM2TrackAction } from './action';

const builder: ComponentBuilder = ({ core }) => {
    const action = new SAM2TrackAction(core);
    const registration = core.actions.register(action);
    // Do not hide failures as a successfully initialized tracker.
    void registration.catch((error: unknown) => console.error('SAM2 action registration failed', error));
    return {
        name: 'SAM2 tracking',
        destructor: async () => {
            await registration;
            await core.actions.unregister(action);
            await action.destroy();
        },
    };
};
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
