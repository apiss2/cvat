// SPDX-License-Identifier: MIT
import type { ComponentBuilder, PluginEntryPoint } from 'components/plugins-entrypoint';
import { createRefresher } from './refresh';

const builder: ComponentBuilder = ({ core, store, dispatch, actionCreators }) => {
    // The v2.75 header menu calls this factory to obtain an Ant Design menu item.
    const menu = (() => ({
        key: 'model-registry',
        label: 'モデル管理',
        onClick: (): void => { window.open('/model-registry/#models', '_blank', 'noopener,noreferrer'); },
    })) as unknown as Parameters<typeof actionCreators.addUIComponent>[1];
    dispatch(actionCreators.addUIComponent('header.userMenu.items', menu, { weight: 25 }));
    const identity = (): string => String(store.getState().auth.user?.id ?? '');
    const refresher = createRefresher({
        identity,
        visible: () => !document.hidden,
        load: async () => (await core.lambda.list()).models,
        // Registry revisions have immutable IDs. Do not replace model instances on every poll.
        fingerprint: (models) => JSON.stringify(models.map((model) => [model.id, model.name, model.version])
            .sort((left, right) => String(left[0]).localeCompare(String(right[0])))),
        commit: (models) => dispatch(actionCreators.getModelsSuccess(models, models.length)),
        failed: (error) => console.warn('Model list refresh failed; keeping the current list', error),
    });
    const refresh = (): void => { void refresher.refresh(); };
    const timer = window.setInterval(refresh, 5000);
    let lastIdentity = identity();
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    refresh();
    return {
        name: 'Model registry list refresh',
        globalStateDidUpdate: (): void => {
            const next = identity();
            if (next !== lastIdentity) { lastIdentity = next; refresh(); }
        },
        destructor: (): void => {
            dispatch(actionCreators.removeUIComponent('header.userMenu.items', menu));
            refresher.dispose();
            window.clearInterval(timer);
            window.removeEventListener('focus', refresh);
            document.removeEventListener('visibilitychange', refresh);
        },
    };
};
let registered = false;
function register(): void {
    const host = (window as unknown as { cvatUI?: { registerComponent: PluginEntryPoint } }).cvatUI;
    if (host && !registered) { registered = true; host.registerComponent(builder); }
}
window.addEventListener('plugins.ready', register, { once: true });
register();
