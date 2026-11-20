// Thin reporting and wall-clock bridge for Vidal's explicit matrix C API.
#include "C_Interface.h"
#include "Individual.h"
#include <chrono>
#include <vector>

using Clock = std::chrono::steady_clock;
using Reporter = void (*)(const int *, int);
static thread_local Reporter report = nullptr;
static thread_local Clock::time_point deadline;

bool hgs_before_deadline() { return Clock::now() < deadline; }

void hgs_new_best(const Individual &best) {
    if (!report) return;
    std::vector<int> sequence;
    for (const auto &route : best.chromR) {
        if (route.empty()) continue;
        sequence.insert(sequence.end(), route.begin(), route.end());
        sequence.push_back(0);
    }
    report(sequence.data(), static_cast<int>(sequence.size()));
}

extern "C" int hgs_run_matrix(int n, double *x, double *y, double *costs,
                               double *service, double *load, double capacity,
                               int seed, double seconds, Reporter callback) {
    auto params = default_algorithm_parameters();
    params.seed = seed;
    params.timeLimit = seconds;
    deadline = Clock::now() + std::chrono::duration_cast<Clock::duration>(
        std::chrono::duration<double>(seconds));
    report = callback;
    // The upstream final feasibility check always inspects durationLimit,
    // even for CVRP. Use its unbounded sentinel and disable duration search.
    auto *sol = solve_cvrp_dist_mtx(n, x, y, costs, service, load, capacity,
                                    1.e30, 0, n - 1, &params, 0);
    report = nullptr;
    if (!sol) return -1;
    int routes = sol->n_routes;
    delete_solution(sol);
    return routes;
}
