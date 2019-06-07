# Copyright (c) 2019 Foundry.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##############################################################################

from __future__ import print_function

import sys
import os
import time
import random
import argparse
from datetime import datetime

import scipy.misc
import numpy as np

import tensorflow as tf
from util.model_builder import EncoderDecoder
from util.util import im2uint8, get_filepaths_from_dir, get_ckpt_list, read_exr, print_

class TrainModel(object):
    """Train the EncoderDecoder from the given input and grountruth data"""

    def __init__(self, args):
        self.n_levels = 3
        self.scale = 0.5
        self.channels = 3 # input / output channels
        self.checkpoints_dir = './checkpoints'
        if not os.path.exists(self.checkpoints_dir):
            os.makedirs(self.checkpoints_dir)
        self.save_name = 'trainingTemplateTF.model'
        # Directory containing tensorboard summaries
        self.summaries_dir = './summaries/'

        # Get training dataset as lists of image paths
        in_data_path = './data/train/input'
        gt_data_path = './data/train/groundtruth'
        self.in_data_list = get_filepaths_from_dir(in_data_path)
        self.gt_data_list = get_filepaths_from_dir(gt_data_path)
        if len(self.in_data_list) is 0 or len(self.gt_data_list) is 0:
            raise ValueError("No training data found in folders {} or {}".format(in_data_path, gt_data_path))
        if len(self.in_data_list) != len(self.gt_data_list):
            raise ValueError("{} and {} should have the same number of input data".format(in_data_path, gt_data_path))
        self.file_extension = os.path.splitext(self.in_data_list[0])[1][1:]

        # Get testing dataset if provided
        self.has_test_data = True
        test_in_data_path = './data/test/input'
        test_gt_data_path = './data/test/groundtruth'
        self.test_in_data_list = get_filepaths_from_dir(test_in_data_path)
        self.test_gt_data_list = get_filepaths_from_dir(test_gt_data_path)
        if len(self.test_in_data_list) is 0 or len(self.test_gt_data_list) is 0:
            print("No test data found in {} or {}".format(test_in_data_path, test_gt_data_path))
            self.has_test_data = False
        elif len(self.test_in_data_list) != len(self.test_gt_data_list):
            raise ValueError("{} and {} should have the same number of input data".format(test_in_data_path, test_gt_data_path))
        else:
            print("Number of test data: {}".format(len(self.test_in_data_list)))
        
        # Get training hyperparameters
        self.learning_rate = args.learning_rate
        self.batch_size = args.batch_size
        self.epoch = args.epoch
        if (len(self.in_data_list) < self.batch_size):
            raise ValueError("Batch size must be smaller than the dataset (batch size = {}, number of training data = {})"
                .format(self.batch_size, len(self.in_data_list)))
        self.crop_size = 256

        batch_per_epoch = (len(self.in_data_list)) // self.batch_size
        self.max_steps = int(self.epoch * (batch_per_epoch))
        print_("Number of training data: {}\nNumber of batches per epoch: {} (batch size = {})\nNumber of training steps for {} epochs: {}\n"
            .format(len(self.in_data_list), batch_per_epoch, self.batch_size, self.epoch, self.max_steps), 'm')

    def get_data(self, in_data_list, gt_data_list, batch_size=16, epoch=100):

        def read_and_preprocess_data(path_img_in, path_img_gt):
            if self.file_extension in ['jpg', 'jpeg', 'png', 'bmp', 'JPG', 'JPEG', 'PNG', 'BMP']:
                img_in_raw = tf.read_file(path_img_in)
                img_gt_raw = tf.read_file(path_img_gt)
                img_in_tensor = tf.image.decode_image(img_in_raw, channels=3)
                img_gt_tensor = tf.image.decode_image(img_gt_raw, channels=3)
                # Normalise
                imgs = [tf.cast(img, tf.float32) / 255.0 for img in [img_in_tensor, img_gt_tensor]]
            elif self.file_extension in ['exr', 'EXR']:
                img_in = tf.py_func(read_exr, [path_img_in], tf.float32)
                img_gt = tf.py_func(read_exr, [path_img_gt], tf.float32)
                img_in_tensor = tf.convert_to_tensor(img_in, dtype=tf.float32)
                img_gt_tensor = tf.convert_to_tensor(img_gt, dtype=tf.float32)
                imgs = [tf.cast(img, tf.float32) for img in [img_in_tensor, img_gt_tensor]]
            else:
                raise TypeError("{} unhandled type extensions. Should be one of "
                    "['jpg', 'jpeg', 'png', 'bmp', 'exr']". format(self.file_extension))
            # Crop data
            img_crop = tf.unstack(tf.random_crop(tf.stack(imgs, axis=0), [2, self.crop_size, self.crop_size, self.channels]), axis=0)
            return img_crop
        
        with tf.variable_scope('input'):
            # Ensure preprocessing is done on the CPU (to let the GPU focus on training)
            with tf.device('/cpu:0'):
                in_list = tf.convert_to_tensor(self.in_data_list, dtype=tf.string)
                gt_list = tf.convert_to_tensor(self.gt_data_list, dtype=tf.string)
        
                path_dataset = tf.data.Dataset.from_tensor_slices((in_list, gt_list))
                path_dataset = path_dataset.shuffle(buffer_size=len(self.in_data_list)).repeat(epoch)
                # Apply read_and_preprocess_data function to all input in the path_dataset
                dataset = path_dataset.map(read_and_preprocess_data, num_parallel_calls=4)
                dataset = dataset.batch(batch_size)
                # Always prefetch one batch and make sure there is always one ready
                dataset = dataset.prefetch(buffer_size=1)
                # Create operator to iterate over the created dataset
                next_element = dataset.make_one_shot_iterator().get_next()
                return next_element
    
    def loss(self, n_outputs, img_gt):
        """Compute multi-scale loss function"""
        loss_total = 0
        for i in xrange(self.n_levels):
            _, hi, wi, _ = n_outputs[i].shape
            gt_i = tf.image.resize_images(img_gt, [hi, wi], method=0)
            loss = tf.reduce_mean(tf.square(gt_i - n_outputs[i]))
            loss_total += loss
            # Save out images and loss values to tensorboard
            tf.summary.image('out_' + str(i), im2uint8(n_outputs[i]))
        # Save total loss to tensorboard
        tf.summary.scalar('loss_total', loss_total)
        return loss_total

    def test(self, model):
        total_test_loss = 0.0
        # Get next data from preprocessed test dataset
        test_img_in, test_img_gt = self.get_data(self.test_in_data_list, self.test_gt_data_list, self.batch_size, 1)
        n_outputs = model(test_img_in, reuse=False)
        test_op = self.loss(n_outputs, test_img_gt)
        # Test results over one epoch
        batch_per_epoch = len(self.test_in_data_list) // self.batch_size
        for batch in xrange(batch_per_epoch):
            total_test_loss += test_op
        return total_test_loss / batch_per_epoch

    def train(self):    
        # Build model
        model = EncoderDecoder(self.n_levels, self.scale, self.channels)

        # Learning rate decay
        global_step = tf.Variable(initial_value=0, dtype=tf.int32, trainable=False)
        self.lr = tf.train.polynomial_decay(self.learning_rate, global_step, self.max_steps, end_learning_rate=0.0,
                                            power=0.3)
        tf.summary.scalar('learning_rate', self.lr)
        # Training operator
        adam = tf.train.AdamOptimizer(self.lr)

        # Get next data from preprocessed training dataset
        img_in, img_gt = self.get_data(self.in_data_list, self.gt_data_list, self.batch_size, self.epoch)
        tf.summary.image('img_in', im2uint8(img_in))
        tf.summary.image('img_gt', im2uint8(img_gt))
        print('img_in, img_gt', img_in.shape, img_gt.shape)
        # Compute image loss
        n_outputs = model(img_in, reuse=False)
        loss_op = self.loss(n_outputs, img_gt)
        # By default, adam uses the current graph trainable_variables to optimise training,
        # thus train_op should be the last operation of the graph for training.
        train_op = adam.minimize(loss_op, global_step)

        # Create session
        sess = tf.Session(config=tf.ConfigProto(gpu_options=tf.GPUOptions(allow_growth=True)))
        # Initialise all the variables in current session
        init = tf.global_variables_initializer()
        sess.run(init)
        self.saver = tf.train.Saver(max_to_keep=100, keep_checkpoint_every_n_hours=1)

        # Check if there are intermediate trained model to load
        if not self.load(sess, self.checkpoints_dir):
            print_("Starting training from scratch\n", 'm')

        # Tensorboard summary
        summary_op = tf.summary.merge_all()
        summary_name = "data{}_bch{}_ep{}".format(len(self.in_data_list), self.batch_size, self.epoch)
        summary_writer = tf.summary.FileWriter(self.summaries_dir + summary_name, graph=sess.graph, flush_secs=30)

        # Testing on unseen test dataset
        if self.has_test_data:
            test_loss_op = self.test(model)
            # Save test loss to tensorboard
            test_summary_op = tf.summary.scalar('test_loss', test_loss_op)

        for step in xrange(sess.run(global_step), self.max_steps):
            start_time = time.time()
            # Train model and record summaries
            if step % 100 == 0 or step == self.max_steps - 1:
                _, loss_total, summary = sess.run([train_op, loss_op, summary_op])
                summary_writer.add_summary(summary, global_step=step)
            else: # Train only
                _, loss_total = sess.run([train_op, loss_op])
            duration = time.time() - start_time
            assert not np.isnan(loss_total), 'Model diverged with loss = NaN'

            if step % 10 == 0 or step == self.max_steps - 1:
                examples_per_sec = self.batch_size / duration
                sec_per_batch = float(duration)
                format_str = ('%s: step %d, loss = %.5f (%.1f data/s; %.3f s/bch)')
                print(format_str % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), step, loss_total,
                                examples_per_sec, sec_per_batch))

            if step % 1000 == 0 or step == self.max_steps - 1:
                # Save current model in a checkpoint
                self.save(sess, self.checkpoints_dir, step)
                # Compute loss on unseen test dataset to check overfitting
                if self.has_test_data:
                    test_loss, test_summary = sess.run([test_loss_op, test_summary_op])
                    summary_writer.add_summary(test_summary, global_step=step)
                    print("Loss on test dataset: {}".format(test_loss))

        print_("--------End of training--------\n", 'm')
        # Free all resources associated with the session
        sess.close()

    def save(self, sess, checkpoint_dir, step):
        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)
        self.saver.save(sess, os.path.join(checkpoint_dir, self.save_name), global_step=step)

    def load(self, sess, checkpoint_dir):
        ckpt_names = get_ckpt_list(checkpoint_dir)
        if not ckpt_names: # list is empty
            print_("No checkpoints found in {}\n".format(checkpoint_dir), 'm')
            return False
        else:
            print_("Found checkpoints:\n", 'm')
            for name in ckpt_names:
                print("    {}".format(name))
            # Ask user if they prefer to start training from scratch or resume training on a specific ckeckpoint 
            while True:
                mode=str(raw_input('Start training from scratch (start) or resume training from a previous checkpoint (choose one of the above): '))
                if mode == 'start' or mode in ckpt_names:
                    break
                else:
                    print("Answer should be 'start' or one of the following checkpoints: {}".format(ckpt_names))
                    continue
            if mode == 'start':
                return False
            elif mode in ckpt_names:
                # Try to load given intermediate checkpoint
                print_("Loading trained model...\n", 'm')          
                self.saver.restore(sess, os.path.join(checkpoint_dir, mode))
                print_("...Checkpoint {} loaded\n".format(mode), 'm')
                return True
            else:
                raise ValueError("User input is neither 'start' nor a valid checkpoint")

def parse_args():
    parser = argparse.ArgumentParser(description='Model training arguments')
    parser.add_argument('--bch', type=int, default=16, dest='batch_size', help='training batch size')
    parser.add_argument('--ep', type=int, default=10000, dest='epoch', help='training epoch number')
    parser.add_argument('--lr', type=float, default=1e-4, dest='learning_rate', help='initial learning rate')
    args = parser.parse_args()
    return args

if __name__ == '__main__':
    args = parse_args()
    # set up model to train
    model = TrainModel(args)
    model.train()