"""
Usage:
python3 -m unittest -bv test_solver_mlp.py
"""
from collections import defaultdict
from enum import Enum
import unittest

import numpy as np

from hlo import *
from cluster_env import ClusterEnvironment
from solver import solve_auto_sharding, SolverOption


def assert_close(x, y):
    assert abs(x / y - 1) < 0.001, f"{x} vs. {y}"


class MLPSolverTest(unittest.TestCase):
    def test_mlp_2_layer_forward(self):
        # Build Hlo Computation
        batch_size = 128
        input_dim = hidden_dim = output_dim = 1024

        computation = HloComputation()

        with computation:
            x = HloParameter((batch_size, input_dim))
            w1 = HloParameter((input_dim, hidden_dim))
            w2 = HloParameter((hidden_dim, output_dim))
            h1 = HloDot(x, w1)
            h2 = HloDot(h1, w2)
            h2 = HloExp(h2)
            out = HloTuple((h2, w1, w2))

        # Solve
        cluster_env = ClusterEnvironment(num_devices=4, memory_per_device=4 * 1024**2)
        objective = solve_auto_sharding(computation, cluster_env)

        expected = cluster_env.all_gather_cost(batch_size * hidden_dim * 4)
        print("Objective:", objective)
        print("Expected:", expected)
        assert_close(objective, expected)

    def test_mlp_2_layer_forward_backward(self):
        # Build Hlo Computation
        batch_size = 128
        input_dim = hidden_dim = output_dim = 1024

        computation = HloComputation()
        with computation:
            x = HloParameter((batch_size, input_dim))
            y = HloParameter((batch_size, output_dim))
            w1 = HloParameter((input_dim, hidden_dim))
            w2 = HloParameter((hidden_dim, output_dim))

            # forward
            h1 = HloDot(x, w1)
            h2 = HloDot(h1, w2)
            loss = HloSubtract(h2, y)

            # backward
            coef = HloConstant(2 / batch_size / output_dim)
            coef = HloBroadcast(coef, (batch_size, output_dim))
            grad_loss = HloMutiply(loss, coef)

            grad_w2 = HloDot(h1, grad_loss,
                             lhs_contracting_dims=(0,),
                             rhs_contracting_dims=(0,),)
            new_w2 = HloSubtract(w2, grad_w2)
            grad_h2 = HloDot(grad_loss, w2,
                             lhs_contracting_dims=(1,),
                             rhs_contracting_dims=(1,),)

            grad_w1 = HloDot(x, grad_h2,
                             lhs_contracting_dims=(0,),
                             rhs_contracting_dims=(0,),)
            new_w1 = HloSubtract(w1, grad_w1)
            out = HloTuple((new_w1, new_w2))

            # alias
            computation.set_alias([(w1, new_w1), (w2, new_w2)])

        """
         0: parameter.0 (128, 1024) = parameter()
         1: parameter.1 (128, 1024) = parameter()
         2: parameter.2 (1024, 1024) = parameter()
         3: parameter.3 (1024, 1024) = parameter()
         4: dot.0 (128, 1024) = dot(parameter.0, parameter.2)  lhs_con_dim=(1,), rhs_con_dim=(0,)
         5: dot.1 (128, 1024) = dot(dot.0, parameter.3)  lhs_con_dim=(1,), rhs_con_dim=(0,)
         6: subtract.0 (128, 1024) = subtract(dot.1, parameter.1)
         7: constant.0 () = constant(1.52587891e-05)
         8: broadcast.0 (128, 1024) = broadcast(constant.0)
         9: multiply.0 (128, 1024) = multiply(subtract.0, broadcast.0)
        10: dot.2 (1024, 1024) = dot(dot.0, multiply.0)  lhs_con_dim=(0,), rhs_con_dim=(0,)
        11: subtract.1 (1024, 1024) = subtract(parameter.2, dot.2)
        12: dot.3 (128, 1024) = dot(multiply.0, parameter.3)  lhs_con_dim=(1,), rhs_con_dim=(1,)
        13: dot.4 (1024, 1024) = dot(parameter.0, dot.3)  lhs_con_dim=(0,), rhs_con_dim=(0,)
        14: subtract.2 (1024, 1024) = subtract(parameter.2, dot.4)
        15: tuple.0 () = tuple('subtract.2', 'subtract.1') 
        """

        # | method                        | peak mem | communication cost          |
        # | ----------------------------- | -------- | --------------------------- |
        # | data-parallel                 | 16.3 M   | 2 all-reduce                |
        # | megatron-LM                   | 5.00 M   | 1 all-reduce                |
        # | megatron-LM + naive partition | 4.62 M   | 1 all-reduce + 2 all-gather |
        # | ours new finding              | 4.25 M   | 2 all-reduce                |
        cluster_env = ClusterEnvironment(num_devices=4, memory_per_device=5 * 1024**2)
        objective = solve_auto_sharding(computation, cluster_env)

        expected = cluster_env.all_reduce_cost(batch_size * hidden_dim * 4)
        print("Objective:", objective)
        print("Expected:", expected)
        assert_close(objective, expected)

    def test_mlp_n_layer_forward(self):
        # Build Hlo Computation
        batch_size = 128
        input_dim = hidden_dim = output_dim = 1024
        num_layers = 6

        computation = HloComputation()
        with computation:
            x = HloParameter((batch_size, input_dim))
            w_first = HloParameter((input_dim, hidden_dim))
            w_inter = []
            for i in range(num_layers - 2):
                w_inter.append(HloParameter((hidden_dim, hidden_dim)))
            w_last = HloParameter((hidden_dim, output_dim))

            h_first = HloDot(x, w_first)
            h_now = h_first
            for i in range(num_layers - 2):
                h_now = HloDot(h_now, w_inter[i])
            h_last = HloDot(h_now, w_last)
            h_last = HloExp(h_last)
            out = HloTuple([h_last, w_first, w_last] + w_inter)

        # Solve
        cluster_env = ClusterEnvironment(num_devices=4, memory_per_device=8 * 1024**2)
        solve_auto_sharding(computation, cluster_env)

    def test_mlp_n_layer_forward_backward(self):
        # Build Hlo Computation
        batch_size = 128
        input_dim = hidden_dim = output_dim = 1024
        num_layers = 10

        computation = HloComputation()
        with computation:
            x = HloParameter((batch_size, input_dim))
            y = HloParameter((batch_size, output_dim))
            w_first = HloParameter((input_dim, hidden_dim))
            w_inter = []
            for i in range(num_layers - 2):
                manual_strategy = "S0" if i % 2 == 0 else "S1"
                w_inter.append(HloParameter((hidden_dim, hidden_dim)))
            w_last = HloParameter((hidden_dim, output_dim))

            # forward
            h_first = HloDot(x, w_first)
            h_now = h_first
            h_inter = []
            for i in range(num_layers - 2):
                h_now = HloDot(h_now, w_inter[i])
                h_inter.append(h_now)
            h_last = HloDot(h_now, w_last)

            loss = HloSubtract(h_last, y)

            # backward
            coef = HloConstant(2 / batch_size / output_dim)
            coef = HloBroadcast(coef, (batch_size, output_dim))
            grad_loss = HloMutiply(loss, coef)
            grad_h_now = grad_loss

            grad_w_last = HloDot(h_inter[-1], grad_h_now,
                                 lhs_contracting_dims=(0,),
                                 rhs_contracting_dims=(0,),)
            new_w_last = HloSubtract(w_last, grad_w_last)
            grad_h_now = HloDot(grad_h_now, w_last,
                                 lhs_contracting_dims=(1,),
                                 rhs_contracting_dims=(1,),)

            new_w_inter = []
            for i in range(num_layers - 3, -1, -1):
                grad_w = HloDot(h_inter[i-1], grad_h_now,
                                lhs_contracting_dims=(0,),
                                rhs_contracting_dims=(0,),)
                new_w = HloSubtract(w_inter[i], grad_w)
                grad_h_now = HloDot(grad_h_now, w_inter[i],
                                    lhs_contracting_dims=(1,),
                                    rhs_contracting_dims=(1,),)
                new_w_inter.append(new_w)

            grad_w_first = HloDot(x, grad_h_now,
                                  lhs_contracting_dims=(0,),
                                  rhs_contracting_dims=(0,),)
            new_w_first = HloSubtract(w_first, grad_w_first)

            out = HloTuple([new_w_first] + new_w_inter + [new_w_last])

            # alias
            alias_list = [(w_first, new_w_first), (w_last, new_w_last)] +\
                [(w_old, w_new) for w_old, w_new in zip(w_inter, reversed(new_w_inter))]
            computation.set_alias(alias_list)

        # Solve
        cluster_env = ClusterEnvironment(num_devices=4, memory_per_device=20 * 1024**2)
        objective = solve_auto_sharding(computation, cluster_env)


        print("Objective:", int(objective))
        print("Expected:", int(cluster_env.all_reduce_cost(batch_size * hidden_dim * 4) * (num_layers - 1)))
        assert_close(objective,
            cluster_env.all_reduce_cost(batch_size * hidden_dim * 4) * (num_layers - 1))


    def test_mlp_2_layer_forward_backward_force_data_parallel(self):
        # Build Hlo Computation
        batch_size = 128
        input_dim = hidden_dim = output_dim = 1024

        computation = HloComputation()
        with computation:
            x = HloParameter((batch_size, input_dim))
            y = HloParameter((batch_size, output_dim))
            w1 = HloParameter((input_dim, hidden_dim))
            w2 = HloParameter((hidden_dim, output_dim))

            # forward
            h1 = HloDot(x, w1)
            h2 = HloDot(h1, w2)
            loss = HloSubtract(h2, y)

            # backward
            coef = HloConstant(2 / batch_size / output_dim)
            coef = HloBroadcast(coef, (batch_size, output_dim))
            grad_loss = HloMutiply(loss, coef)

            grad_w2 = HloDot(h1, grad_loss,
                             lhs_contracting_dims=(0,),
                             rhs_contracting_dims=(0,),)
            new_w2 = HloSubtract(w2, grad_w2)
            grad_h2 = HloDot(grad_loss, w2,
                             lhs_contracting_dims=(1,),
                             rhs_contracting_dims=(1,),)

            grad_w1 = HloDot(x, grad_h2,
                             lhs_contracting_dims=(0,),
                             rhs_contracting_dims=(0,),)
            new_w1 = HloSubtract(w1, grad_w1)
            out = HloTuple((new_w1, new_w2))

            # alias
            computation.set_alias([(w1, new_w1), (w2, new_w2)])

        solver_option = SolverOption()
        solver_option.force_data_parallel = True
        solver_option.force_all_reduce_cost = 1000
        cluster_env = ClusterEnvironment(num_devices=4, memory_per_device=20 * 1024**2,
                                         solver_option=solver_option)

        objective = solve_auto_sharding(computation, cluster_env, solver_option)
        expected = solver_option.force_all_reduce_cost * 2
        print("Objective:", objective)
        print("Expected:", expected)
        assert_close(objective, expected)


    def test_mlp_2_layer_forward_backward_force_zero_data_parallel(self):
        # Build Hlo Computation
        batch_size = 128
        input_dim = hidden_dim = output_dim = 1024

        computation = HloComputation()
        with computation:
            x = HloParameter((batch_size, input_dim))
            y = HloParameter((batch_size, output_dim))
            w1 = HloParameter((input_dim, hidden_dim))
            w2 = HloParameter((hidden_dim, output_dim))
            w1_aux = HloParameter((input_dim, hidden_dim))
            w2_aux = HloParameter((hidden_dim, output_dim))

            # forward
            h1 = HloDot(x, w1)
            h2 = HloDot(h1, w2)
            loss = HloSubtract(h2, y)

            # backward
            coef = HloConstant(2 / batch_size / output_dim)
            coef = HloBroadcast(coef, (batch_size, output_dim))
            grad_loss = HloMutiply(loss, coef)

            grad_w2 = HloDot(h1, grad_loss,
                             lhs_contracting_dims=(0,),
                             rhs_contracting_dims=(0,),)
            new_w2_aux = HloMutiply(w2_aux, grad_w2)
            new_w2 = HloSubtract(w2, new_w2_aux)

            grad_h2 = HloDot(grad_loss, w2,
                             lhs_contracting_dims=(1,),
                             rhs_contracting_dims=(1,),)

            grad_w1 = HloDot(x, grad_h2,
                             lhs_contracting_dims=(0,),
                             rhs_contracting_dims=(0,),)
            new_w1_aux = HloMutiply(w1_aux, grad_w1)
            new_w1 = HloSubtract(w1, new_w1_aux)

            out = HloTuple((new_w1, new_w2, new_w1_aux, new_w2_aux))

            # alias
            computation.set_alias([(w1, new_w1), (w2, new_w2),
                                   (w1_aux, new_w1_aux), (w2_aux, new_w2_aux)])

        solver_option = SolverOption()
        solver_option.force_data_parallel = True
        solver_option.force_reduce_scatter_cost = 1000
        cluster_env = ClusterEnvironment(num_devices=4, memory_per_device=100 * 1024**2,
                                         solver_option=solver_option)

        objective = solve_auto_sharding(computation, cluster_env, solver_option)
        expected = cluster_env.all_gather_cost(4 * hidden_dim * hidden_dim) * 2
        print("Objective:", objective)
        print("Expected:", expected)
        assert_close(objective, expected)


def suite():
    suite = unittest.TestSuite()
    suite.addTest(MLPSolverTest('test_mlp_2_layer_forward'))
    suite.addTest(MLPSolverTest('test_mlp_n_layer_forward'))
    suite.addTest(MLPSolverTest('test_mlp_2_layer_forward_backward'))
    suite.addTest(MLPSolverTest('test_mlp_n_layer_forward_backward'))
    suite.addTest(MLPSolverTest('test_mlp_2_layer_forward_backward_force_data_parallel'))
    suite.addTest(MLPSolverTest('test_mlp_2_layer_forward_backward_force_zero_data_parallel'))
    return suite


if __name__ == '__main__':
    runner = unittest.TextTestRunner()
    runner.run(suite())
