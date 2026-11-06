// SPDX-License-Identifier: MIT
const encoder = new TextEncoder();

export function safePath(name: string): string {
    // eslint-disable-next-line no-control-regex -- Reject control characters in uploaded paths.
    if (!name || /^[A-Za-z]:/.test(name) || name.startsWith('/') || /[\\\x00-\x1f]/.test(name) ||
        name.split('/').some((part) => ['', '.', '..'].includes(part))) throw new Error('安全でないファイル名です。');
    if (encoder.encode(name).length > 1024) throw new Error('ファイル名が長すぎます。');
    return name;
}

function checked(signal: AbortSignal): void {
    if (signal.aborted) throw new Error('操作を中止しました。');
}

function collector(signal: AbortSignal): {
    files: File[]; add: (file: File, path?: string) => Promise<void>;
} {
    const files: File[] = [];
    return {
        files,
        add: async (file, path): Promise<void> => {
            checked(signal);
            if (!/\.(dcm|dicom|zip|tar|tar\.gz|tgz)$/i.test(file.name)) {
                if (file.size < 132) return;
                const signature = new Uint8Array(await file.slice(128, 132).arrayBuffer());
                checked(signal);
                if (signature[0] !== 68 || signature[1] !== 73 || signature[2] !== 67 || signature[3] !== 77) return;
            }
            const relativePath = safePath(path || file.webkitRelativePath || file.name);
            if (!Number.isSafeInteger(file.size) || file.size < 0) throw new Error('ファイルサイズが不正です。');
            if (path && path !== file.webkitRelativePath) {
                const selected = new File([file], file.name, { type: file.type, lastModified: file.lastModified });
                Object.defineProperty(selected, 'webkitRelativePath', { value: relativePath });
                files.push(selected);
            } else {
                files.push(file);
            }
        },
    };
}

export async function collectFiles(files: readonly File[], signal: AbortSignal): Promise<File[]> {
    checked(signal);
    const selection = collector(signal);
    for (const file of files) await selection.add(file);
    checked(signal);
    return selection.files;
}

export async function collectDroppedFiles(dataTransfer: DataTransfer, signal: AbortSignal): Promise<File[]> {
    checked(signal);
    // Capture entries and File objects before the drop event's data store becomes protected.
    const captured = Array.from(dataTransfer.items || [])
        .filter((item) => item.kind === 'file')
        .map((item) => item.webkitGetAsEntry?.() || item.getAsFile?.())
        .filter((source): source is FileSystemEntry | File => source !== null && source !== undefined);
    const sources: (FileSystemEntry | File)[] = captured.length ? captured : Array.from(dataTransfer.files);
    const selection = collector(signal);
    const visit = async (entry: FileSystemEntry, parent = ''): Promise<void> => {
        checked(signal);
        const path = safePath(parent ? `${parent}/${entry.name}` : entry.name);
        if (entry.isFile) {
            const file = await new Promise<File>((resolve, reject) => {
                (entry as FileSystemFileEntry).file(resolve, reject);
            });
            await selection.add(file, path);
        } else if (entry.isDirectory) {
            const reader = (entry as FileSystemDirectoryEntry).createReader();
            // Chromium can return only 100 entries at a time. Keep the same reader until exhausted.
            let entries: FileSystemEntry[];
            do {
                checked(signal);
                entries = await new Promise<FileSystemEntry[]>((resolve, reject) => {
                    reader.readEntries(resolve, reject);
                });
                checked(signal);
                for (const child of entries) await visit(child, path);
            } while (entries.length);
        }
    };
    for (const source of sources) {
        if ('isFile' in source) await visit(source);
        else await selection.add(source);
    }
    checked(signal);
    return selection.files;
}
