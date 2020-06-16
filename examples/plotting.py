"""
This is an example script that creates a simple game and three solvers.
The plots in this script require that the matplotlib package be installed.
"""

import random
import matplotlib.pyplot as plt

from bbench.games import LambdaGame
from bbench.solvers import RandomSolver, EpsilonAverageSolver
from bbench.benchmarks import UniversalBenchmark

#define a game
game = LambdaGame(lambda i: None, lambda s: [0,1,2,3,4], lambda s,a: random.uniform(a-2, a+2))

#create three different solvers
randomsolver_factory = lambda: RandomSolver()
averagesolver_factory1 = lambda: EpsilonAverageSolver(1/10, lambda a: 0)
averagesolver_factory2 = lambda: EpsilonAverageSolver(1/10, lambda a: 10)

#define a benchmark
benchmark = UniversalBenchmark([game], lambda i: 1, 100)

#benchmark all three solvers
random_result   = benchmark.evaluate(randomsolver_factory)
average_result1 = benchmark.evaluate(averagesolver_factory1)
average_result2 = benchmark.evaluate(averagesolver_factory2)

fig = plt.figure()

ax = fig.add_subplot(1,1,1)

ax.plot([ i.mean for i in random_result.progressive_stats]  , label="random")
ax.plot([ i.mean for i in average_result1.progressive_stats], label="pessimistic epsilon-greedy")
ax.plot([ i.mean for i in average_result2.progressive_stats], label="optimistic epsilon-greedy")

ax.set_title("Mean Observed Reward for Iterations <= ")
ax.set_ylabel("Mean Reward")
ax.set_xlabel("Iteration <=")

ax.legend()
plt.show()