// SPDX-License-Identifier: MIT
export interface RefreshOptions<T> {
    identity: () => string;
    visible: () => boolean;
    load: () => Promise<T[]>;
    fingerprint: (models: T[]) => string;
    commit: (models: T[]) => void;
    failed: (error: unknown) => void;
}

export function createRefresher<T>(options: RefreshOptions<T>): {
    refresh: () => Promise<void>;
    dispose: () => void;
} {
    let disposed = false;
    let inFlight = false;
    let signature = '';
    let lastIdentity = '';
    let errorReported = false;
    return {
        refresh: async (): Promise<void> => {
            const identity = options.identity();
            if (disposed || inFlight || !identity || !options.visible()) return;
            inFlight = true;
            try {
                const models = await options.load();
                if (disposed || identity !== options.identity()) return;
                const next = options.fingerprint(models);
                if (next !== signature || identity !== lastIdentity) {
                    signature = next;
                    lastIdentity = identity;
                    options.commit(models);
                }
                errorReported = false;
            } catch (error: unknown) {
                // Preserve the existing model list on transport/authorization errors.
                if (!disposed && identity === options.identity() && !errorReported) {
                    options.failed(error);
                    errorReported = true;
                }
            } finally {
                inFlight = false;
            }
        },
        dispose: (): void => { disposed = true; },
    };
}
