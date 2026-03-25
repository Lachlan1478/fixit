import random
import time

results = []
for n in (10_000, 100_000):
    numbers = [random.random() for _ in range(n)]
    start = time.perf_counter()
    numbers.sort()
    elapsed = time.perf_counter() - start
    results.append((n, numbers[0], numbers[-1], elapsed * 1000))

print(f"{'Size':>10}  {'Min':>10}  {'Max':>10}  {'Time (ms)':>10}")
print("-" * 48)
for n, mn, mx, ms in results:
    print(f"{n:>10,}  {mn:>10.6f}  {mx:>10.6f}  {ms:>10.3f}")
