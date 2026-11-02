// SPDX-License-Identifier: MIT
export const DICOM_FORMAT = 'Annotation Tags DICOM Import 1.0';

export interface ImportSummary {
    succeeded: number;
    failed: number;
    unmatched: number;
    invalid: number;
}

export function readSummary(message: unknown): ImportSummary | undefined {
    if (typeof message !== 'string') return undefined;
    const match = /Project annotations: (\d+) tasks succeeded; (\d+) tasks failed; (\d+) unmatched records(?:; (\d+) invalid records)?/.exec(message);
    if (!match) return undefined;
    const values = match.slice(1).map((value) => Number(value || 0));
    if (!values.every(Number.isSafeInteger)) return undefined;
    return { succeeded: values[0], failed: values[1], unmatched: values[2], invalid: values[3] };
}

export function summaryMessage(summary: ImportSummary): string {
    return `成功: ${summary.succeeded} Task、失敗: ${summary.failed} Task、未一致: ${summary.unmatched} 件` +
        (summary.invalid ? `、入力不備: ${summary.invalid} 件` : '');
}

export class ImportError extends Error {}

export function errorMessage(reason: unknown): string {
    // Server/network errors may contain filenames or DICOM identifiers. Only our own
    // fixed messages and the parsed numeric result are suitable for notifications.
    if (reason instanceof ImportError) return reason.message;
    const summary = readSummary(reason instanceof Error ? reason.message : reason);
    return summary ? summaryMessage(summary) : '取り込み結果を確認できませんでした。Requestsで状況を確認してください。';
}
