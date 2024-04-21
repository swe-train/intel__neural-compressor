import os
import shutil
import unittest

import tensorflow as tf
import yaml
from tensorflow.compat.v1 import graph_util

from neural_compressor.tensorflow.algorithms.static_quant.tensorflow import TensorFlowAdaptor
from neural_compressor.tensorflow.utils import disable_random


class TestQuantizeInput(unittest.TestCase):
    @classmethod
    def tearDownClass(self):
        shutil.rmtree("./saved", ignore_errors=True)

    @disable_random()
    @unittest.skipIf(
        tf.version.VERSION < "2.1.0",
        "Quantize input needs tensorflow 2.1.0 and newer, so test_quantize_input is skipped",
    )
    def test_quantize_input(self):
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        paddings = tf.constant([[0, 0], [1, 1], [1, 1], [0, 0]])
        x_pad = tf.pad(x, paddings, "CONSTANT")
        conv_weights = tf.compat.v1.get_variable(
            "weight", [3, 3, 16, 16], initializer=tf.compat.v1.random_normal_initializer()
        )
        conv = tf.nn.conv2d(x_pad, conv_weights, strides=[1, 2, 2, 1], padding="VALID")

        conv_bias = tf.compat.v1.get_variable("bias", [16], initializer=tf.compat.v1.random_normal_initializer())

        conv_bias = tf.math.add(conv, conv_bias)
        relu6 = tf.nn.relu6(conv_bias, name="op_to_store")

        out_name = relu6.name.split(":")[0]
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            constant_graph = graph_util.convert_variables_to_constants(
                sess=sess, input_graph_def=sess.graph_def, output_node_names=[out_name]
            )

            for i in constant_graph.node:
                if i.op.find("Add") != -1:
                    i.op = "Add"

            from neural_compressor.tensorflow import StaticQuantConfig, quantize_model
            from neural_compressor.tensorflow.utils import BaseDataLoader, DummyDataset

            dataset = DummyDataset(shape=(100, 56, 56, 16), label=True)
            calib_dataloader = BaseDataLoader(dataset)
            quant_config = StaticQuantConfig()
            q_model = quantize_model(constant_graph, quant_config, calib_dataloader)

            framework_specific_info = {
                "device": "cpu",
                "approach": "post_training_static_quant",
                "random_seed": 1978,
                "inputs": ["input"],
                "outputs": ["op_to_store"],
                "workspace_path": "saved",
                "format": "default",
                "backend": "default",
            }

            quantize_input_graph, _ = TensorFlowAdaptor(framework_specific_info).quantize_input(q_model.graph)
            Not_found_QuantizedV2 = True
            for i in quantize_input_graph.as_graph_def().node:
                if i.op == "QuantizeV2":
                    Not_found_QuantizedV2 = False
                    break

            self.assertEqual(Not_found_QuantizedV2, True)


if __name__ == "__main__":
    unittest.main()
