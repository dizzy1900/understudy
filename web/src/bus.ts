import type { EventOf, EventType, StreamEvent } from "./types";

type Handler<T extends EventType> = (event: EventOf<T>) => void;
type AnyHandler = (event: StreamEvent) => void;

const handlers = new Map<string, Set<AnyHandler>>();

/** One-shot stream events for the scene and the overlays. State lives in the store; this is for reactions
 * (a stamp, a flash, a particle burst) that should not be derived by diffing state. */
export function on<T extends EventType>(type: T, handler: Handler<T>): () => void {
  let set = handlers.get(type);
  if (!set) {
    set = new Set();
    handlers.set(type, set);
  }
  const wrapped = handler as unknown as AnyHandler;
  set.add(wrapped);
  return () => {
    set!.delete(wrapped);
  };
}

export function emit(event: StreamEvent): void {
  const set = handlers.get(event.type);
  if (!set) return;
  for (const handler of [...set]) {
    try {
      handler(event);
    } catch (error) {
      console.error("bus handler failed", error);
    }
  }
}
