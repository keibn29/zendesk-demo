interface ZafClient {
  get(paths: string | string[]): Promise<Record<string, unknown>>;
  invoke(name: string, ...args: unknown[]): unknown;
  on(event: string, handler: (data?: unknown) => void): void;
  off?(event: string, handler: (data?: unknown) => void): void;
}

interface Window {
  ZAFClient?: { init(): ZafClient };
}
