// SPDX-License-Identifier: MIT
import { safePath } from './archive';

function checked(signal: AbortSignal): void {
    if (signal.aborted) throw new Error('操作を中止しました。');
}

function collector(signal: AbortSignal): {
    files: File[]; add: (file: File, path?: string) => Promise<void>;
} {
    const files: File[] = [];
    let bytes = 0;
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
            if (files.length >= 50000) throw new Error('1～50,000ファイルを選択してください。');
            bytes += file.size;
            if (bytes > 2 * 1024 ** 3) throw new Error('1回のアップロードは2 GiBまでです。');
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
