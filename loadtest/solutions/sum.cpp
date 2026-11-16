#include <cstdio>

int main() {
    int n;
    long long sum = 0, x;
    if (scanf("%d", &n) != 1) return 1;
    for (int i = 0; i < n; i++) {
        scanf("%lld", &x);
        sum += x;
    }
    printf("%lld\n", sum);
}
