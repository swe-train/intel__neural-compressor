
import unittest
import os
import yaml
import neural_compressor
from neural_compressor.adaptor.tf_utils.util import disable_random
from neural_compressor.adaptor.tf_utils.quantize_graph.quantize_graph_for_intel_cpu import QuantizeGraphForIntel
from neural_compressor.adaptor.tensorflow import TensorflowQuery

import tensorflow as tf
from tensorflow.compat.v1 import graph_util

def build_fake_yaml():
    fake_yaml = '''
        model:
          name: fake_yaml
          framework: tensorflow
          inputs: input
          outputs: op_to_store
        device: cpu
        quantization:
          model_wise:
            weight:
                granularity: per_tensor
                scheme: sym
                dtype: int8
                algorithm: minmax
            activation:
                algorithm: kl
        evaluation:
          accuracy:
            metric:
              topk: 1
        tuning:
            strategy:
              name: mse
            accuracy_criterion:
              relative: 0.01
            exit_policy:
              performance_only: True
            workspace:
              path: saved
        '''
    y = yaml.load(fake_yaml, Loader=yaml.SafeLoader)
    with open('fake_yaml.yaml', "w", encoding="utf-8") as f:
        yaml.dump(y, f)
    f.close()

class TestTensorflowGraphInsertLogging(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        build_fake_yaml()

    @classmethod
    def tearDownClass(self):
        os.remove('fake_yaml.yaml')

    @disable_random()
    def test_graph_insert_logging(self):
        x = tf.compat.v1.placeholder(tf.float32, [1, 56, 56, 16], name="input")
        top_relu = tf.nn.relu(x)

        conv_weights = tf.compat.v1.get_variable("weight2", [3, 3, 16, 16],
                                                  initializer=tf.compat.v1.random_normal_initializer())
        conv = tf.nn.conv2d(top_relu, conv_weights, strides=[1, 2, 2, 1], padding="SAME")
        normed = tf.nn.bias_add(conv, tf.constant([3.0, 1.2,1,2,3,4,5,6,7,8,9,0,12,2,3,4]), name='op_to_store')

        out_name = normed.name.split(':')[0]
        with tf.compat.v1.Session() as sess:
            sess.run(tf.compat.v1.global_variables_initializer())
            output_graph_def = graph_util.convert_variables_to_constants(
                sess=sess,
                input_graph_def=sess.graph_def,
                output_node_names=[out_name])

            inputs = [x.name.split(':')[0]]
            outputs = [out_name]
            op_wise_config = {
                "Conv2D": (False, 'minmax', False, 7.0),
            }
            op_wise_sequences = TensorflowQuery(local_config_file=os.path.join(
                os.path.dirname(neural_compressor.__file__), "adaptor/tensorflow.yaml")).get_eightbit_patterns()
            output_graph, _, _ = QuantizeGraphForIntel(output_graph_def, inputs, outputs,
                                    op_wise_config, op_wise_sequences, 'cpu').do_transform()

            offset_map = {
                "QuantizedConv2DWithBiasSumAndRelu": 3,
                "QuantizedConv2DWithBiasAndRelu": 2,
                "QuantizedConv2DWithBias": 1,
            }
            target_conv_op = []
            _print_node_mapping = {}
            from neural_compressor.adaptor.tf_utils.quantize_graph_common import QuantizeGraphHelper
            sorted_graph = QuantizeGraphHelper().get_sorted_graph(output_graph, inputs, outputs)

            for node in output_graph.node:
                if node.op in offset_map:
                    target_conv_op.append(node.name.split('_eightbit_')[0])

            node_name_mapping = {
                node.name: node for node in output_graph.node if node.op != "Const"
            }

            output_node_names = []
            for i in target_conv_op:
                if node_name_mapping[i + "_eightbit_quantized_conv"].op == \
                        'QuantizedConv2DWithBias':
                    output_node_names.append(node_name_mapping[i + "_eightbit_quantized_conv"].name)

            from neural_compressor.adaptor.tf_utils.transform_graph.insert_logging import InsertLogging
            graph_def = InsertLogging(output_graph,
                      node_name_list=output_node_names,
                      message="__KL:",
                      summarize=-1,
                      dump_fp32=False).do_transformation()

            found_conv_fusion = False

            for i in output_graph.node:
                if i.op.find('QuantizedConv2D') != -1:
                    found_conv_fusion = True
                    break

            self.assertEqual(found_conv_fusion, True)

if __name__ == "__main__":
    unittest.main()
