import sys

n, *numbers = map(int, sys.stdin.buffer.read().split())
print(sum(numbers[:n]))
