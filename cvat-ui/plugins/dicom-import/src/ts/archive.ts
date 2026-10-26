// SPDX-License-Identifier: MIT
// ZIP STORE writer: keep File objects as Blob slices; read only 1 MiB at a time for CRC32.
const encoder = new TextEncoder();
/* eslint-disable no-bitwise -- CRC32 operates on individual bits. */
const table = new Uint32Array(256);
for (let index = 0; index < 256; index++) {
    let value = index;
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ ((value & 1) ? 0xEDB88320 : 0);
    table[index] = value;
}
export function crc32(data: Uint8Array, previous = 0): number {
    let crc = previous ^ 0xFFFFFFFF;
    for (const value of data) crc = table[(crc ^ value) & 255] ^ (crc >>> 8);
    return (crc ^ 0xFFFFFFFF) >>> 0;
}
/* eslint-enable no-bitwise */
export function safePath(name: string): string {
    // eslint-disable-next-line no-control-regex -- Reject control characters in archive paths.
    if (!name || /^[A-Za-z]:/.test(name) || name.startsWith('/') || /[\\\x00-\x1f]/.test(name) ||
        name.split('/').some((part) => ['', '.', '..'].includes(part))) throw new Error('安全でないファイル名です。');
    if (encoder.encode(name).length > 1024) throw new Error('ファイル名が長すぎます。');
    return name;
}
function checked(signal: AbortSignal): void {
    if (signal.aborted) throw new Error('操作を中止しました。');
}
function header(length: number): [Uint8Array<ArrayBuffer>, DataView] {
    const bytes = new Uint8Array(length);
    return [bytes, new DataView(bytes.buffer)];
}
export async function pack(files: File[], prefix: string, signal: AbortSignal,
    progress: (value: number) => void = () => {}): Promise<File> {
    if (!prefix.trim() || encoder.encode(prefix).length > 256) throw new Error('接頭辞を指定してください。');
    if (!files.length || files.length > 50000) throw new Error('1～50,000ファイルを選択してください。');
    const manifest = '_cvat_dicom_import.json';
    const entries: { name: string; data: Blob }[] = [{
        name: manifest,
        data: new Blob([JSON.stringify({ version: 1, prefix })]),
    }];
    const seen = new Set([manifest]);
    let total = 0;
    for (const file of files) {
        const name = safePath(file.webkitRelativePath || file.name);
        const key = name.toLowerCase();
        if (seen.has(key)) throw new Error('重複するファイル名があります。');
        seen.add(key);
        total += file.size;
        if (total > 2 * 1024 ** 3) throw new Error('1回のアップロードは2 GiBまでです。');
        entries.push({ name, data: file });
    }
    const parts: BlobPart[] = [];
    const central: Uint8Array<ArrayBuffer>[] = [];
    let offset = 0;
    let done = 0;
    for (const entry of entries) {
        checked(signal);
        let crc = 0;
        for (let start = 0; start < entry.data.size; start += 1024 ** 2) {
            checked(signal);
            crc = crc32(new Uint8Array(await entry.data.slice(start, start + 1024 ** 2).arrayBuffer()), crc);
            checked(signal);
        }
        const name = encoder.encode(entry.name);
        const [local, view] = header(30 + name.length);
        view.setUint32(0, 0x04034B50, true); view.setUint16(4, 20, true);
        view.setUint16(6, 0x800, true); view.setUint16(12, 33, true);
        view.setUint32(14, crc, true); view.setUint32(18, entry.data.size, true);
        view.setUint32(22, entry.data.size, true); view.setUint16(26, name.length, true);
        local.set(name, 30);
        parts.push(local, entry.data);
        const [record, directory] = header(46 + name.length);
        directory.setUint32(0, 0x02014B50, true); directory.setUint16(4, 20, true);
        directory.setUint16(6, 20, true); directory.setUint16(8, 0x800, true);
        directory.setUint16(14, 33, true); directory.setUint32(16, crc, true);
        directory.setUint32(20, entry.data.size, true); directory.setUint32(24, entry.data.size, true);
        directory.setUint16(28, name.length, true); directory.setUint32(42, offset, true);
        record.set(name, 46); central.push(record);
        offset += local.length + entry.data.size;
        progress(++done / entries.length);
    }
    const [end, view] = header(22);
    view.setUint32(0, 0x06054B50, true); view.setUint16(8, entries.length, true);
    view.setUint16(10, entries.length, true);
    view.setUint32(12, central.reduce((sum, item) => sum + item.length, 0), true);
    view.setUint32(16, offset, true);
    checked(signal);
    return new File([...parts, ...central, end], 'dicom-import.zip', { type: 'application/zip' });
}
