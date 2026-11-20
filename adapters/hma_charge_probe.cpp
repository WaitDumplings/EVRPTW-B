#include "solution.h"
#include <fstream>
#include <iomanip>

// Call the same evaluator that HMA uses to qualify its search incumbents.
int main(int argc, char **argv) {
    if (argc != 4) return 2;
    ArgumentParser parser;
    parser.addArgument("--problem", 1, false);
    parser.addArgument("--benchmark_reference");
    char self[] = "charge_probe", problem[] = "--problem", reference[] = "--benchmark_reference";
    char *args[] = {self, problem, argv[1], reference};
    parser.parse(4, args);
    Data data(parser);
    int station = std::stoi(argv[2]);
    if (station < 0 || station >= data.node_num || data.node[station].type != 2) return 3;
    std::vector<int> route{data.DC, station, data.DC};
    std::vector<status> states;
    int flag = 0, negative = -1;
    double cost = 0;
    reference_route_status(route, states, data, flag, cost, negative, false);
    if (flag != 1) return 4;
    auto stop = states[1];
    double rate = data.vehicle.consumption_rate;
    std::ofstream result(argv[3]);
    result << std::setprecision(17) << (states[0].dep_RD - stop.arr_RD)*rate << ' '
           << (stop.dep_time - stop.arr_time) << ' ' << stop.dep_RD*rate << '\n';
}
