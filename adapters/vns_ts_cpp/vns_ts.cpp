// VNS/TS for the E-VRPTW after Schneider, Stenger and Goeke (2014), Transportation Science 48(4).
// Single-threaded C++17. Reads the explicit-matrix file written by the EVRPTW-B electric adapter
// and writes one JSON line per improved feasible solution.
//
// Usage: vns_ts INPUT --events FILE [--seed S] [--budget SECONDS | --deadline-monotonic T]
//               [--granular K] [--eta-tabu N] [--eta-dist N] [--eta-feas N] [--init-time-check 0|1]
//        vns_ts INPUT --probe STATION      prints: energy_kwh charge_seconds usable_kwh_on_departure
//                                          for the arc depot -> STATION, using the search evaluator's rules
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <random>
#include <sstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>
using namespace std;

static double now_s() {
    return chrono::duration<double>(chrono::steady_clock::now().time_since_epoch()).count();
}

struct Inst {
    int n = 0, m = 0, N = 0;            // customers, stations, all nodes (depot = 0)
    vector<double> D, T, E;             // km, seconds, kWh; row-major N*N
    vector<double> dem, ready, due, serv, x, y, rate;  // rate: seconds per kWh at a station
    double C = 0, Q = 0;
    string name;
    bool cust(int v) const { return v >= 1 && v <= n; }
    bool stat(int v) const { return v > n; }
    double d(int a, int b) const { return D[(size_t)a * N + b]; }
    double t(int a, int b) const { return T[(size_t)a * N + b]; }
    double e(int a, int b) const { return E[(size_t)a * N + b]; }
};

static vector<string> split(const string& s, char c) {
    vector<string> out; string cur; stringstream ss(s);
    while (getline(ss, cur, c)) out.push_back(cur);
    return out;
}

static Inst read_instance(const string& path) {
    ifstream f(path);
    if (!f) { cerr << "cannot open " << path << "\n"; exit(2); }
    Inst I; string line; string section;
    vector<char> kind;
    while (getline(f, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty()) continue;
        auto colon = line.find(" : ");
        if (colon != string::npos && section.empty()) {
            string key = line.substr(0, colon), val = line.substr(colon + 3);
            if (key == "NAME") I.name = val;
            else if (key == "DIMENSION") I.N = stoi(val);
            else if (key == "CAPACITY") I.C = stod(val);
            else if (key == "ELECTRIC_POWER") I.Q = stod(val);
            continue;
        }
        if (line == "NODE_SECTION" || line == "DISTANCETIME_SECTION" ||
            line == "STATION_CHARGING_SECONDS_PER_KWH" || line == "DEPOT_SECTION") {
            section = line;
            if (section == "NODE_SECTION") {
                I.dem.assign(I.N, 0); I.ready.assign(I.N, 0); I.due.assign(I.N, 0);
                I.serv.assign(I.N, 0); I.x.assign(I.N, 0); I.y.assign(I.N, 0);
                I.rate.assign(I.N, 0); kind.assign(I.N, '?');
            }
            if (section == "DISTANCETIME_SECTION") {
                I.D.assign((size_t)I.N * I.N, 0); I.T = I.D; I.E = I.D;
            }
            continue;
        }
        if (line.rfind("ID,", 0) == 0) continue;  // header row
        auto p = split(line, ',');
        if (section == "NODE_SECTION") {
            int i = stoi(p[0]); kind[i] = p[1][0];
            I.x[i] = stod(p[2]); I.y[i] = stod(p[3]); I.dem[i] = stod(p[4]);
            I.ready[i] = stod(p[6]); I.due[i] = stod(p[7]); I.serv[i] = stod(p[8]);
        } else if (section == "DISTANCETIME_SECTION") {
            int a = stoi(p[1]), b = stoi(p[2]);
            size_t k = (size_t)a * I.N + b;
            I.D[k] = stod(p[3]); I.T[k] = stod(p[4]); I.E[k] = stod(p[5]);
        } else if (section == "STATION_CHARGING_SECONDS_PER_KWH") {
            I.rate[stoi(p[0])] = stod(p[1]);
        }
    }
    for (int i = 1; i < I.N; i++) {
        if (kind[i] == 'c') { if (I.m) { cerr << "customers must precede stations\n"; exit(2); } I.n++; }
        else if (kind[i] == 'f') I.m++;
        else { cerr << "unexpected node kind\n"; exit(2); }
    }
    if (kind[0] != 'd' || I.n + I.m + 1 != I.N) { cerr << "bad node section\n"; exit(2); }
    return I;
}

struct Cost { double dist = 0, cap = 0, tw = 0, batt = 0; };

struct Params {
    int eta_feas = 500, eta_dist = 200, eta_tabu = 100, eta_penalty = 2;
    double delta_sa = 0.08, pen0 = 10, pen_min = 0.5, pen_max = 5000, pen_delta = 1.2;
    int tabu_min = 15, tabu_max = 30;
    double lambda_div = 1.0;
    int granular = 0;          // 0: every arc kept by the preprocessing
    int init_time_check = 1;   // open a new route in the construction when the time penalty appears
};

struct Move { int type = -1, r1 = -1, i = 0, r2 = -1, j = 0, s = 0; };
enum { M_2OPT, M_RELOC, M_EXCH, M_STIN, M_STRM };

struct Solver {
    const Inst& I; Params P; mt19937_64 rng;
    double deadline; string events; double started;
    // Violations are expressed in distance-equivalents so that the paper's weights apply.
    double sc_cap = 1, sc_tw = 1, sc_batt = 1;
    double al, be, ga;
    vector<vector<int>> R; vector<Cost> RC;
    vector<int> rt, ix;                       // customer -> route, index
    vector<vector<int>> gen;                  // generator arcs: gen[v] = list of w
    unordered_map<uint64_t, long> tabu;       // (u, v, route) -> first iteration it is free again
    unordered_map<uint64_t, long> freq;       // (vertex, route) -> number of additions
    unordered_set<uint64_t> cur_arcs;
    long iter = 0, moves_done = 0, evals = 0;
    int cap_streak = 0, tw_streak = 0, batt_streak = 0;
    double best_feas = 1e300; vector<vector<int>> best_routes;
    vector<int> b1, b2;
    long restarts = 0, vns_iters = 0;

    Solver(const Inst& inst, Params p, uint64_t seed, double dl, string ev, double st)
        : I(inst), P(p), rng(seed), deadline(dl), events(std::move(ev)), started(st) {}

    bool expired() const { return now_s() >= deadline; }
    uint64_t akey(int u, int v, int r) const { return ((uint64_t)u * I.N + v) * 4096ULL + (uint64_t)r; }
    uint64_t fkey(int u, int r) const { return (uint64_t)u * 4096ULL + (uint64_t)r; }

    Cost eval(const vector<int>& s) const {
        Cost c; if (s.empty()) return c;
        double load = 0, t = I.ready[0], en = 0; int prev = 0;
        for (size_t k = 0; k <= s.size(); k++) {
            int v = k < s.size() ? s[k] : 0;
            c.dist += I.d(prev, v); en += I.e(prev, v); t += I.t(prev, v);
            if (k == s.size()) {                       // back at the depot
                if (en > I.Q) c.batt += en - I.Q;
                if (t > I.due[0]) c.tw += t - I.due[0];
            } else {
                if (t < I.ready[v]) t = I.ready[v];
                if (t > I.due[v]) { c.tw += t - I.due[v]; t = I.due[v]; }   // travel back in time
                if (I.stat(v)) {
                    if (en > I.Q) c.batt += en - I.Q;
                    t += I.rate[v] * min(en, I.Q);     // recharge completely
                    en = 0;
                } else { load += I.dem[v]; t += I.serv[v]; }
            }
            prev = v;
        }
        if (load > I.C) c.cap = load - I.C;
        return c;
    }
    double gen_cost(const Cost& c) const {
        return c.dist + al * sc_cap * c.cap + be * sc_tw * c.tw + ga * sc_batt * c.batt;
    }
    Cost total() const { Cost s; for (auto& c : RC) { s.dist += c.dist; s.cap += c.cap; s.tw += c.tw; s.batt += c.batt; } return s; }
    static bool feasible(const Cost& c) { return c.cap <= 1e-6 && c.tw <= 1e-7 && c.batt <= 1e-9; }
    int used() const { int k = 0; for (auto& r : R) k += !r.empty(); return k; }

    void reindex() {
        bool spare = false;
        for (auto& r : R) spare |= r.empty();
        if (!spare) { R.emplace_back(); RC.emplace_back(); }
        if (R.size() >= 4096) { cerr << "too many routes\n"; exit(3); }
        cur_arcs.clear();
        for (int r = 0; r < (int)R.size(); r++) {
            int prev = 0;
            for (int k = 0; k < (int)R[r].size(); k++) {
                int v = R[r][k];
                if (I.cust(v)) { rt[v] = r; ix[v] = k; }
                cur_arcs.insert((uint64_t)prev * I.N + v); prev = v;
            }
            if (!R[r].empty()) cur_arcs.insert((uint64_t)prev * I.N);
        }
    }
    void recost() { RC.resize(R.size()); for (size_t r = 0; r < R.size(); r++) RC[r] = eval(R[r]); }

    // ---- preprocessing (13)-(16) and generator arcs -------------------------------------------
    void build_generators() {
        int N = I.N; gen.assign(N, {});
        vector<double> to_re(N, 1e300), from_re(N, 1e300);   // cheapest energy to / from a recharge point
        for (int v = 0; v < N; v++)
            for (int j = 0; j < N; j++) if (j != v && !I.cust(j)) {
                to_re[v] = min(to_re[v], I.e(v, j)); from_re[v] = min(from_re[v], I.e(j, v));
            }
        for (int v = 0; v < N; v++) {
            vector<pair<double, int>> cand;
            for (int w = 0; w < N; w++) {
                if (w == v) continue;
                if (I.cust(v) && I.cust(w)) {
                    if (I.dem[v] + I.dem[w] > I.C) continue;                               // (13)
                    if (from_re[v] + I.e(v, w) + to_re[w] > I.Q) continue;                 // (16)
                }
                if (v != 0 || true) {
                    double dep = I.ready[v] + (I.cust(v) ? I.serv[v] : 0);
                    if (w != 0 && dep + I.t(v, w) > I.due[w]) continue;                    // (14)
                    if (I.cust(w) && dep + I.t(v, w) + I.serv[w] + I.t(w, 0) > I.due[0]) continue;  // (15)
                }
                cand.push_back({I.d(v, w), w});
            }
            if (P.granular > 0 && (int)cand.size() > P.granular) {
                // The depot and the stations stay reachable from every vertex.
                nth_element(cand.begin(), cand.begin() + P.granular, cand.end());
                vector<pair<double, int>> keep(cand.begin(), cand.begin() + P.granular);
                for (size_t k = P.granular; k < cand.size(); k++)
                    if (!I.cust(cand[k].second)) keep.push_back(cand[k]);
                cand.swap(keep);
            }
            for (auto& c : cand) gen[v].push_back(c.second);
        }
    }

    // ---- initial solution (Section 4.1) ---------------------------------------------------------
    void initial() {
        R.clear(); RC.clear();
        uniform_real_distribution<double> U(0, 1);
        double minx = 1e300, maxx = -1e300, miny = 1e300, maxy = -1e300;
        for (int v = 0; v < I.N; v++) { minx = min(minx, I.x[v]); maxx = max(maxx, I.x[v]); miny = min(miny, I.y[v]); maxy = max(maxy, I.y[v]); }
        double px = minx + U(rng) * (maxx - minx), py = miny + U(rng) * (maxy - miny);
        double base = atan2(py - I.y[0], px - I.x[0]);
        vector<pair<double, int>> order;
        for (int c = 1; c <= I.n; c++) {
            double a = atan2(I.y[c] - I.y[0], I.x[c] - I.x[0]) - base;
            while (a < 0) a += 2 * M_PI;
            order.push_back({a, c});
        }
        sort(order.begin(), order.end());
        vector<int> route; double load = 0;
        auto energy_no_recharge = [&](const vector<int>& s) { double en = 0; int p = 0; for (int v : s) { en += I.e(p, v); p = v; } return en + I.e(p, 0); };
        for (auto& oc : order) {
            int u = oc.second; int best = -1; double bd = 1e300;
            for (int pos = 0; pos <= (int)route.size(); pos++) {
                int a = pos ? route[pos - 1] : 0, b = pos < (int)route.size() ? route[pos] : 0;
                if (pos && I.ready[a] > I.ready[u]) continue;                 // e_i <= e_u <= e_j
                if (pos < (int)route.size() && I.ready[u] > I.ready[b]) continue;
                double inc = I.d(a, u) + I.d(u, b) - I.d(a, b);
                if (inc < bd) { bd = inc; best = pos; }
            }
            if (best < 0) best = (int)route.size();
            vector<int> trial = route; trial.insert(trial.begin() + best, u);
            bool viol = load + I.dem[u] > I.C || energy_no_recharge(trial) > I.Q;
            if (!viol && P.init_time_check) viol = eval(trial).tw > 1e-7;
            if (viol && !route.empty()) { R.push_back(route); route.assign(1, u); load = I.dem[u]; }
            else { route.swap(trial); load += I.dem[u]; }
        }
        if (!route.empty()) R.push_back(route);
        rt.assign(I.N, -1); ix.assign(I.N, -1);
        recost(); reindex(); recost();
    }

    // ---- candidate construction ---------------------------------------------------------------
    // Fills b1 (new route r1) and, for two-route moves, b2 (new route r2).
    bool build(const Move& mv) {
        const vector<int>& A = R[mv.r1];
        b1.clear(); b2.clear();
        if (mv.type == M_2OPT) {
            const vector<int>& B = R[mv.r2];
            b1.insert(b1.end(), A.begin(), A.begin() + (mv.i + 1));
            b1.insert(b1.end(), B.begin() + mv.j, B.end());
            b2.insert(b2.end(), B.begin(), B.begin() + mv.j);
            b2.insert(b2.end(), A.begin() + (mv.i + 1), A.end());
            return true;
        }
        if (mv.type == M_RELOC) {            // vertex at (r2, j) goes behind index i of r1
            const vector<int>& B = R[mv.r2]; int v = B[mv.j];
            if (mv.r1 == mv.r2) {
                if (mv.i == mv.j || mv.i == mv.j - 1) return false;
                b1 = A; b1.erase(b1.begin() + mv.j);
                int at = mv.i < mv.j ? mv.i + 1 : mv.i;
                b1.insert(b1.begin() + at, v);
                return true;
            }
            b1 = A; b1.insert(b1.begin() + (mv.i + 1), v);
            b2 = B; b2.erase(b2.begin() + mv.j);
            return true;
        }
        if (mv.type == M_EXCH) {
            const vector<int>& B = R[mv.r2];
            if (mv.r1 == mv.r2) { if (mv.i == mv.j) return false; b1 = A; swap(b1[mv.i], b1[mv.j]); return true; }
            b1 = A; b2 = B; swap(b1[mv.i], b2[mv.j]); return true;
        }
        if (mv.type == M_STIN) { b1 = A; b1.insert(b1.begin() + mv.i, mv.s); return true; }
        if (mv.type == M_STRM) { b1 = A; b1.erase(b1.begin() + mv.i); return true; }
        return false;
    }
    bool two_routes(const Move& mv) const { return mv.type == M_2OPT || ((mv.type == M_RELOC || mv.type == M_EXCH) && mv.r1 != mv.r2); }

    bool is_tabu(const vector<int>& s, int r) const {
        int prev = 0;
        for (size_t k = 0; k <= s.size(); k++) {
            int v = k < s.size() ? s[k] : 0;
            if (!cur_arcs.count((uint64_t)prev * I.N + v)) {
                auto it = tabu.find(akey(prev, v, r));
                if (it != tabu.end() && it->second > iter) return true;
            }
            prev = v;
        }
        return false;
    }
    double added_frequency(const vector<int>& s, int r) const {
        double sum = 0;
        for (int v : s) {
            bool added = I.cust(v) ? rt[v] != r : true;
            if (!added) continue;
            auto it = freq.find(fkey(v, r));
            if (it != freq.end()) sum += (double)it->second;
        }
        return sum;
    }

    // ---- one tabu-search iteration: best non-tabu move of the composite neighbourhood -----------
    bool ts_step(double cur_gen, const Cost& cur_total) {
        Move best; double best_val = 1e300; bool have = false;
        int nveh = max(1, used());
        double div_scale = P.lambda_div * sqrt((double)(I.n + I.m) * nveh) / (double)max(1L, moves_done);
        auto consider = [&](const Move& mv) {
            if (!build(mv)) return;
            evals++;
            bool two = two_routes(mv);
            Cost c1 = eval(b1), c2 = two ? eval(b2) : Cost();
            Cost t = cur_total;
            t.dist += c1.dist - RC[mv.r1].dist; t.cap += c1.cap - RC[mv.r1].cap;
            t.tw += c1.tw - RC[mv.r1].tw; t.batt += c1.batt - RC[mv.r1].batt;
            if (two) {
                t.dist += c2.dist - RC[mv.r2].dist; t.cap += c2.cap - RC[mv.r2].cap;
                t.tw += c2.tw - RC[mv.r2].tw; t.batt += c2.batt - RC[mv.r2].batt;
            }
            double g = gen_cost(t), val = g;
            if (val >= best_val) return;
            bool aspir = feasible(t) && t.dist < best_feas - 1e-9;
            if (!aspir) {
                if (is_tabu(b1, mv.r1) || (two && is_tabu(b2, mv.r2))) return;
                if (g >= cur_gen) {
                    double f = added_frequency(b1, mv.r1) + (two ? added_frequency(b2, mv.r2) : 0);
                    val = g + div_scale * t.dist * f;
                    if (val >= best_val) return;
                }
            }
            best = mv; best_val = val; have = true;
        };
        // Station visits currently in the solution.
        vector<vector<pair<int, int>>> occ(I.N);
        for (int r = 0; r < (int)R.size(); r++)
            for (int k = 0; k < (int)R[r].size(); k++) if (I.stat(R[r][k])) occ[R[r][k]].push_back({r, k});
        int spare = -1;
        for (int r = 0; r < (int)R.size(); r++) if (R[r].empty()) { spare = r; break; }
        vector<pair<int, int>> vpos, wpos;
        for (int v = 0; v < I.N; v++) {
            vpos.clear();
            if (I.cust(v)) vpos.push_back({rt[v], ix[v]}); else if (I.stat(v)) vpos = occ[v];
            for (int w : gen[v]) {
                wpos.clear();
                if (I.cust(w)) wpos.push_back({rt[w], ix[w]}); else if (I.stat(w)) wpos = occ[w];
                if (v == 0) {                                   // arc (depot, w): w becomes a first vertex
                    for (auto& q : wpos) {
                        for (int r = 0; r < (int)R.size(); r++) consider({M_RELOC, r, -1, q.first, q.second, 0});
                        if (spare >= 0 && q.second > 0) consider({M_2OPT, spare, -1, q.first, q.second, 0});
                    }
                    continue;
                }
                if (w == 0) {                                   // arc (v, depot): v becomes a last vertex
                    for (auto& p : vpos) {
                        for (int r = 0; r < (int)R.size(); r++) {
                            consider({M_RELOC, r, (int)R[r].size() - 1, p.first, p.second, 0});
                            if (r != p.first && p.second + 1 < (int)R[p.first].size())
                                consider({M_2OPT, p.first, p.second, r, (int)R[r].size(), 0});
                        }
                    }
                    continue;
                }
                if (I.stat(v) && I.cust(w)) {                   // stationInRe
                    int r = rt[w], k = ix[w];
                    if (k > 0 && R[r][k - 1] == v) consider({M_STRM, r, k - 1, -1, 0, v});
                    else consider({M_STIN, r, k, -1, 0, v});
                }
                for (auto& p : vpos) for (auto& q : wpos) {
                    int r1 = p.first, i = p.second, r2 = q.first, j = q.second;
                    if (r1 == r2 && j == i + 1) continue;       // arc already present
                    consider({M_RELOC, r1, i, r2, j, 0});                       // w behind v
                    if (r1 != r2 || j != i + 1) {                               // v in front of w
                        Move mv{M_RELOC, r2, j - 1, r1, i, 0};
                        consider(mv);
                    }
                    if (r1 != r2) consider({M_2OPT, r1, i, r2, j, 0});
                    if (I.cust(w) && i + 1 < (int)R[r1].size() && I.cust(R[r1][i + 1]))
                        consider({M_EXCH, r1, i + 1, r2, j, 0});               // w takes the place behind v
                    if (I.cust(v) && j > 0 && I.cust(R[r2][j - 1]))
                        consider({M_EXCH, r2, j - 1, r1, i, 0});               // v takes the place before w
                }
            }
        }
        if (!have) return false;
        apply(best);
        return true;
    }

    void arcs_of(const vector<int>& s, vector<pair<int, int>>& out) const {
        int prev = 0; for (int v : s) { out.push_back({prev, v}); prev = v; }
        if (!s.empty()) out.push_back({prev, 0});
    }
    void set_tabu_for_removed(const vector<int>& before, const vector<int>& after, int r) {
        vector<pair<int, int>> a, b; arcs_of(before, a); arcs_of(after, b);
        unordered_set<uint64_t> keep; for (auto& x : b) keep.insert((uint64_t)x.first * I.N + x.second);
        uniform_int_distribution<int> ten(P.tabu_min, P.tabu_max);
        for (auto& x : a) if (!keep.count((uint64_t)x.first * I.N + x.second)) tabu[akey(x.first, x.second, r)] = iter + ten(rng);
    }
    void count_added(const vector<int>& s, int r) {
        for (int v : s) if (I.cust(v) ? rt[v] != r : true) freq[fkey(v, r)]++;
    }
    void apply(const Move& mv) {
        build(mv); bool two = two_routes(mv);
        vector<int> n1 = b1, n2 = b2;
        count_added(n1, mv.r1); if (two) count_added(n2, mv.r2);
        set_tabu_for_removed(R[mv.r1], n1, mv.r1);
        if (two) set_tabu_for_removed(R[mv.r2], n2, mv.r2);
        R[mv.r1].swap(n1); RC[mv.r1] = eval(R[mv.r1]);
        if (two) { R[mv.r2].swap(n2); RC[mv.r2] = eval(R[mv.r2]); }
        reindex(); RC.resize(R.size());
        moves_done++;
    }

    void update_penalties(const Cost& c) {
        auto upd = [&](double v, int& streak, double& w) {
            int s = v > 1e-9 ? 1 : -1;
            streak = (streak * s > 0) ? streak + s : s;
            if (abs(streak) >= P.eta_penalty) {
                w = s > 0 ? min(P.pen_max, w * P.pen_delta) : max(P.pen_min, w / P.pen_delta);
                streak = 0;
            }
        };
        upd(c.cap, cap_streak, al); upd(c.tw, tw_streak, be); upd(c.batt, batt_streak, ga);
    }

    void report(const char* stage) {
        if (events.empty()) return;
        FILE* f = fopen(events.c_str(), "a");
        if (!f) return;
        fprintf(f, "{\"routes\":[");
        bool first = true;
        for (auto& r : best_routes) if (!r.empty()) {
            fprintf(f, "%s[0", first ? "" : ","); first = false;
            for (int v : r) fprintf(f, ",%d", v);
            fprintf(f, ",0]");
        }
        fprintf(f, "],\"internal_objective\":%.17g,\"stage\":\"%s\",\"elapsed_s\":%.6f,\"iteration\":%ld}\n",
                best_feas, stage, now_s() - started, iter);
        fflush(f); fclose(f);
    }
    void note_incumbent(const char* stage) {
        Cost c = total();
        if (feasible(c) && c.dist < best_feas - 1e-9) { best_feas = c.dist; best_routes = R; report(stage); }
    }

    // ---- shaking: cyclic exchange (Table 2) ------------------------------------------------------
    void shake(int kappa) {
        int nr = 2 + (kappa - 1) / 5, lam = 1 + (kappa - 1) % 5;
        vector<int> ids;
        for (int r = 0; r < (int)R.size(); r++) if (!R[r].empty()) ids.push_back(r);
        shuffle(ids.begin(), ids.end(), rng);
        if ((int)ids.size() > nr) ids.resize(nr);
        if (ids.size() < 2) for (int r = 0; r < (int)R.size() && ids.size() < 2; r++) if (R[r].empty()) ids.push_back(r);
        int k = (int)ids.size(); if (k < 2) return;
        vector<vector<int>> before(k), chain(k); vector<int> start(k);
        for (int a = 0; a < k; a++) {
            auto& r = R[ids[a]]; before[a] = r;
            int len = uniform_int_distribution<int>(0, min(lam, (int)r.size()))(rng);
            start[a] = r.empty() ? 0 : uniform_int_distribution<int>(0, (int)r.size() - len)(rng);
            chain[a].assign(r.begin() + start[a], r.begin() + start[a] + len);
            r.erase(r.begin() + start[a], r.begin() + start[a] + len);
        }
        for (int a = 0; a < k; a++) {                 // the chain of route a moves into route a+1
            int b = (a + 1) % k; auto& r = R[ids[b]];
            r.insert(r.begin() + min(start[b], (int)r.size()), chain[a].begin(), chain[a].end());
        }
        for (int a = 0; a < k; a++) { set_tabu_for_removed(before[a], R[ids[a]], ids[a]); }
        recost(); reindex(); RC.resize(R.size());
        for (size_t r = 0; r < R.size(); r++) RC[r] = eval(R[r]);
    }

    // ---- one run of Figure 1 ---------------------------------------------------------------------
    void run_once() {
        initial();
        Cost c0 = total();
        double dref = max(c0.dist, 1e-9);
        double sumD = 0, sumT = 0, sumE = 0;
        for (size_t k = 0; k < I.D.size(); k++) { sumD += I.D[k]; sumT += I.T[k]; sumE += I.E[k]; }
        sc_cap = 0.2 * dref / max(I.C, 1e-12);          // a full vehicle load of excess costs 0.2 x reference distance
        sc_tw = sumT > 0 ? sumD / sumT : 1;              // seconds -> km at the average speed
        sc_batt = sumE > 0 ? sumD / sumE : 1;            // kWh -> km at the average consumption
        al = be = ga = P.pen0; cap_streak = tw_streak = batt_streak = 0;
        tabu.clear(); freq.clear(); moves_done = 0;
        note_incumbent("initial");
        vector<vector<int>> S = R; double fS = gen_cost(total());
        double T0 = -P.delta_sa * max(fS, 1e-9) / log(0.5), temp = T0;
        const int kmax = 15; int kappa = 1; bool feas_phase = true; long i = 0;
        uniform_real_distribution<double> U(0, 1);
        while (!expired() && (feas_phase || i < P.eta_dist)) {
            R = S; recost(); reindex(); RC.resize(R.size()); for (size_t r = 0; r < R.size(); r++) RC[r] = eval(R[r]);
            shake(kappa);
            vector<vector<int>> bestS = R; double bestG = gen_cost(total()); bool bestF = feasible(total());
            double bestD = total().dist;
            for (int tsi = 0; tsi < P.eta_tabu && !expired(); tsi++) {
                iter++;
                Cost cur = total();
                if (!ts_step(gen_cost(cur), cur)) break;
                Cost now = total();
                update_penalties(now);
                note_incumbent(feas_phase ? "feasibility" : "distance");
                bool f = feasible(now); double g = gen_cost(now);
                if ((f && (!bestF || now.dist < bestD - 1e-12)) || (!f && !bestF && g < bestG)) {
                    bestS = R; bestG = g; bestF = f; bestD = now.dist;
                }
            }
            vns_iters++;
            R = bestS; recost(); reindex(); RC.resize(R.size()); for (size_t r = 0; r < R.size(); r++) RC[r] = eval(R[r]);
            Cost s2 = total(); double f2 = gen_cost(s2);
            R = S; recost(); double f1 = gen_cost(total());
            bool accept = f2 < f1 || U(rng) < exp(-(f2 - f1) / max(temp, 1e-12));
            if (accept) { S = bestS; kappa = 1; } else kappa = kappa % kmax + 1;
            R = S; recost(); bool sfeas = feasible(total());
            if (feas_phase) {
                if (sfeas || best_feas < 1e299) { feas_phase = false; i = -1; }
                else if (i == P.eta_feas) i = -1;      // a spare route is always open; no explicit addVehicle
            }
            i++;
            if (!feas_phase) {                        // linear cooling, below 1e-4 in the last 20 % of iterations
                double frac = (double)i / (0.8 * P.eta_dist);
                temp = frac >= 1 ? 1e-5 : max(1e-5, T0 * (1 - frac));
            }
        }
        restarts++;
    }

    void solve() {
        build_generators();
        rt.assign(I.N, -1); ix.assign(I.N, -1);
        while (!expired()) run_once();
    }
};

int main(int argc, char** argv) {
    if (argc < 2) { cerr << "usage: vns_ts INPUT --events FILE [options]\n"; return 2; }
    string input = argv[1], events; uint64_t seed = 1; double budget = 60, deadline = -1; Params P; int probe = -1;
    for (int a = 2; a + 1 < argc; a += 2) {
        string k = argv[a], v = argv[a + 1];
        if (k == "--events") events = v;
        else if (k == "--seed") seed = stoull(v);
        else if (k == "--budget") budget = stod(v);
        else if (k == "--deadline-monotonic") deadline = stod(v);
        else if (k == "--granular") P.granular = stoi(v);
        else if (k == "--eta-tabu") P.eta_tabu = stoi(v);
        else if (k == "--eta-dist") P.eta_dist = stoi(v);
        else if (k == "--eta-feas") P.eta_feas = stoi(v);
        else if (k == "--init-time-check") P.init_time_check = stoi(v);
        else if (k == "--probe") probe = stoi(v);
        else { cerr << "unknown option " << k << "\n"; return 2; }
    }
    double started = now_s();
    if (deadline < 0) deadline = started + budget;
    Inst I = read_instance(input);
    if (probe >= 0) {
        if (!I.stat(probe)) { cerr << "probe target is not a station\n"; return 2; }
        double en = I.e(0, probe);
        printf("%.17g %.17g %.17g\n", en, I.rate[probe] * min(en, I.Q), I.Q);
        return 0;
    }
    Solver S(I, P, seed, deadline, events, started);
    S.solve();
    printf("{\"best_distance_km\":%s,\"runs\":%ld,\"vns_iterations\":%ld,\"ts_iterations\":%ld,\"evaluations\":%ld,\"wall_s\":%.3f}\n",
           S.best_feas < 1e299 ? to_string(S.best_feas).c_str() : "null", S.restarts, S.vns_iters, S.iter, S.evals,
           now_s() - started);
    return 0;
}
