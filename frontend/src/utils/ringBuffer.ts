/** Fixed-capacity ring buffer: chart history never grows browser memory without bound. */
export class RingBuffer<T> {
  private readonly items: (T | undefined)[]
  private start = 0
  private size = 0

  readonly capacity: number

  constructor(capacity: number) {
    this.capacity = capacity
    if (capacity <= 0) throw new Error('capacity must be > 0')
    this.items = new Array<T | undefined>(capacity)
  }

  get length(): number {
    return this.size
  }

  push(item: T): void {
    const index = (this.start + this.size) % this.capacity
    this.items[index] = item
    if (this.size < this.capacity) {
      this.size += 1
    } else {
      this.start = (this.start + 1) % this.capacity
    }
  }

  last(): T | undefined {
    return this.size === 0 ? undefined : this.items[(this.start + this.size - 1) % this.capacity]
  }

  toArray(): T[] {
    const out: T[] = []
    for (let i = 0; i < this.size; i += 1) {
      out.push(this.items[(this.start + i) % this.capacity] as T)
    }
    return out
  }

  /** Items for which `keep` is true, preserving order (used for time-window slicing). */
  filter(keep: (item: T) => boolean): T[] {
    return this.toArray().filter(keep)
  }

  clear(): void {
    this.start = 0
    this.size = 0
    this.items.fill(undefined)
  }
}
