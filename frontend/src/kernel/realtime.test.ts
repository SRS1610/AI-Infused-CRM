import { realtime } from "./realtime";

class FakeSocket {
  static instances: FakeSocket[] = [];
  static OPEN = 1;
  readyState = 0;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
}

describe("realtime client", () => {
  beforeEach(() => vi.stubGlobal("WebSocket", FakeSocket));
  afterEach(() => realtime.disconnect());

  it("subscribes on open, dispatches events and unsubscribes", () => {
    const listener = vi.fn();
    const off = realtime.subscribe("record:x:1", listener);
    realtime.connect();
    const socket = FakeSocket.instances.at(-1)!;
    expect(socket.url).toMatch(/\/api\/v1\/ws$/);
    socket.open();
    expect(JSON.parse(socket.sent[0])).toEqual({ op: "subscribe", channels: ["record:x:1"] });
    socket.onmessage?.({ data: JSON.stringify({ channel: "record:x:1", event: "write", data: { id: 1 } }) });
    socket.onmessage?.({ data: "not json" });
    expect(listener).toHaveBeenCalledWith({ channel: "record:x:1", event: "write", data: { id: 1 } });
    const second = vi.fn();
    const off2 = realtime.subscribe("model:x", second);
    expect(JSON.parse(socket.sent[1])).toEqual({ op: "subscribe", channels: ["model:x"] });
    off();
    expect(JSON.parse(socket.sent[2])).toEqual({ op: "unsubscribe", channels: ["record:x:1"] });
    off2();
  });

  it("reconnects after an unexpected close", () => {
    vi.useFakeTimers();
    realtime.connect();
    const first = FakeSocket.instances.at(-1)!;
    first.onclose?.();
    vi.advanceTimersByTime(600);
    expect(FakeSocket.instances.at(-1)).not.toBe(first);
    vi.useRealTimers();
  });
});
