// WebSocket client for /api/v1/ws with automatic reconnect and channel re-subscription.
import { useEffect } from "react";

export interface RealtimeEvent {
  channel: string;
  event: "create" | "write" | "unlink" | string;
  data: Record<string, any>;
}

type Listener = (event: RealtimeEvent) => void;

class RealtimeClient {
  private socket: WebSocket | null = null;
  private listeners = new Map<string, Set<Listener>>();
  private retry = 0;
  private closedByUser = false;

  connect(): void {
    if (typeof WebSocket === "undefined" || this.socket) return;
    this.closedByUser = false;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${proto}://${window.location.host}/api/v1/ws`);
    this.socket = socket;
    socket.onopen = () => {
      this.retry = 0;
      const channels = [...this.listeners.keys()];
      if (channels.length) socket.send(JSON.stringify({ op: "subscribe", channels }));
    };
    socket.onmessage = (msg) => {
      try {
        const data = JSON.parse(String(msg.data));
        if (data.channel) this.listeners.get(data.channel)?.forEach((fn) => fn(data as RealtimeEvent));
      } catch {
        /* ignore malformed frames */
      }
    };
    socket.onclose = () => {
      this.socket = null;
      if (this.closedByUser) return;
      const delay = Math.min(30_000, 500 * 2 ** this.retry++);
      window.setTimeout(() => this.connect(), delay);
    };
  }

  disconnect(): void {
    this.closedByUser = true;
    this.socket?.close();
    this.socket = null;
  }

  subscribe(channel: string, listener: Listener): () => void {
    let set = this.listeners.get(channel);
    if (!set) {
      set = new Set();
      this.listeners.set(channel, set);
      if (this.socket?.readyState === WebSocket.OPEN)
        this.socket.send(JSON.stringify({ op: "subscribe", channels: [channel] }));
    }
    set.add(listener);
    return () => {
      const current = this.listeners.get(channel);
      current?.delete(listener);
      if (current && current.size === 0) {
        this.listeners.delete(channel);
        if (this.socket?.readyState === WebSocket.OPEN)
          this.socket.send(JSON.stringify({ op: "unsubscribe", channels: [channel] }));
      }
    };
  }
}

export const realtime = new RealtimeClient();

export function useRealtime(channel: string | null, listener: Listener): void {
  useEffect(() => {
    if (!channel) return undefined;
    return realtime.subscribe(channel, listener);
  }, [channel, listener]);
}
