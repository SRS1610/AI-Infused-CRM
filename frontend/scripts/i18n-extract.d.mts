// Types for the extractor helpers imported by src/kernel/i18n.test.ts.
export function extractFromSource(code: string): { keys: string[]; dynamic: number[] };
export function scanSources(srcDir: string): { keys: Map<string, string[]>; dynamic: string[] };
export function placeholders(text: string): string[];
