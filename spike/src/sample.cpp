// A typical contest solution, used to time compilation.
#include <bits/stdc++.h>
using namespace std;

int main() {
    ios::sync_with_stdio(false);
    cin.tie(nullptr);

    int n;
    cin >> n;
    vector<long long> a(n);
    for (auto &x : a) cin >> x;

    sort(a.begin(), a.end());
    long long sum = accumulate(a.begin(), a.end(), 0LL);
    map<long long, int> freq;
    for (auto x : a) freq[x]++;

    cout << "sum " << sum << " distinct " << freq.size() << "\n";
}
