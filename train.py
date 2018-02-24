import numpy as np
import tensorflow as tf
import pdb
import os
import random
import sys
from glob import glob

import metrics

print(f"Numpy version: {np.__version__}")
print(f"Tensorflow version: {tf.__version__}")

tf.logging.set_verbosity(tf.logging.INFO)
random.seed(432)

# Various loading and saving constants.
default_train_dir = "./data/eyepacs/bin2/train"
default_val_dir = "./data/eyepacs/bin2/validation"
default_save_model_path = "./tmp/model.ckpt"
default_save_summaries_dir = "./tmp/logs"

parser = argparse.ArgumentParser(
                    description="Trains and saves neural network for "
                                "detection of diabetic retinopathy.")
parser.add_argument("-t", "--train_dir",
                    help="path to folder that contains training tfrecords",
                    default=default_train_dir)
parser.add_argument("-v", "--val_dir",
                    help="path to folder that contains validation tfrecords",
                    default=default_val_dir)
parser.add_argument("-m", "--save_model_path",
                    help="path to where graph model should be saved",
                    default=default_save_model_path)
parser.add_argument("-s", "--save_summaries_dir",
                    help="path to folder where summaries should be saved",
                    default=default_save_summaries_dir)

args = parser.parse_args()
train_dir = str(args.train_dir)
val_dir = str(args.val_dir)
save_model_path = str(args.save_model_path)
save_summaries_dir = str(args.save_summaries_dir)

# Various training and evaluation constants.
num_channels = 3
num_workers = 8

# Hyper-parameters.
num_epochs = 200
wait_epochs = 10
learning_rate = 3e-3
momentum = 0.9
use_nesterov = True

# Batch sizes.
train_batch_size = 32
val_batch_size = 32

# Buffer size for image shuffling.
shuffle_buffer_size = 5000
prefetch_buffer_size = 100 * train_batch_size

# Set image datas format to channels first if GPU is available.
if tf.test.is_gpu_available():
    print("Found GPU! Using channels first as default image data format.")
    image_data_format = 'channels_first'
else:
    image_data_format = 'channels_last'


def _tfrecord_dataset_from_folder(folder, ext='.tfrecord'):
    tfrecords = [os.path.join(folder, n)
                 for n in os.listdir(folder) if n.endswith(ext)]
    return tf.data.TFRecordDataset(tfrecords)


def _parse_example(proto, image_dim):
    features = {"image/encoded": tf.FixedLenFeature((), tf.string),
                "image/format": tf.FixedLenFeature((), tf.string),
                "image/class/label": tf.FixedLenFeature((), tf.int64),
                "image/height": tf.FixedLenFeature((), tf.int64),
                "image/width": tf.FixedLenFeature((), tf.int64)}
    parsed = tf.parse_single_example(proto, features)

    # Rescale to 1./255.
    image = tf.image.convert_image_dtype(
        tf.image.decode_jpeg(parsed["image/encoded"]), tf.float32)

    image = tf.reshape(image, image_dim)
    label = tf.cast(parsed["image/class/label"], tf.int32)

    return image, label


def initialize_dataset(image_dir, batch_size, num_epochs=1,
                       num_workers=None, prefetch_buffer_size=None,
                       shuffle_buffer_size=None):
    # Retrieve data set from pattern.
    dataset = _tfrecord_dataset_from_folder(image_dir)

    # Find metadata file with image dimensions.
    image_dim_fn = os.path.join(image_dir, 'dimensions.txt')

    # Parse dimensions.txt file in data set folder for image dimensions.
    with open(image_dim_fn, 'r') as f:
        image_dims = list(filter(None, f.read().split('\n')))

        if len(image_dims) > 1:
            raise TypeError(
                "can't initialize dataset with multiple image dims")

        image_dim = [int(x) for x in image_dims[0].split('x')]

    # Specify image shape.
    if image_data_format == 'channels_first':
        image_dim = [num_channels, image_dim[0], image_dim[1]]
    elif image_data_format == 'channels_last':
        image_dim = [image_dim[0], image_dim[1], num_channels]
    else:
        raise TypeError('invalid image date format setting')

    dataset = dataset.map(lambda e: _parse_example(e, image_dim),
                          num_parallel_calls=num_workers)

    if shuffle_buffer_size is not None:
        dataset = dataset.shuffle(shuffle_buffer_size)

    dataset = dataset.repeat(num_epochs)
    dataset = dataset.batch(batch_size)

    if prefetch_buffer_size is not None:
        dataset = dataset.prefetch(prefetch_buffer_size)

    return dataset


# Set up a session and bind it to Keras.
sess = tf.Session()
tf.keras.backend.set_session(sess)
tf.keras.backend.set_learning_phase(True)
tf.keras.backend.set_image_data_format(image_data_format)

# Initialize each data set.
train_dataset = initialize_dataset(
    train_dir, train_batch_size,
    num_workers=num_workers, prefetch_buffer_size=prefetch_buffer_size,
    shuffle_buffer_size=shuffle_buffer_size)

val_dataset = initialize_dataset(
    val_dir, val_batch_size,
    num_workers=num_workers, prefetch_buffer_size=prefetch_buffer_size,
    shuffle_buffer_size=shuffle_buffer_size)

# Create an initialize iterators.
iterator = tf.data.Iterator.from_structure(
    train_dataset.output_types, train_dataset.output_shapes)

images, labels = iterator.get_next()

train_init_op = iterator.make_initializer(train_dataset)
val_init_op = iterator.make_initializer(val_dataset)

# Base model InceptionV3 without top and global average pooling.
base_model = tf.keras.applications.InceptionV3(
    include_top=False, weights='imagenet', pooling='avg', input_tensor=images)

# Add dense layer with the same amount of neurons as labels.
with tf.name_scope('logits'):
    logits = tf.layers.dense(base_model.output, units=num_labels)

# Get the predictions with a sigmoid activation function.
with tf.name_scope('predictions'):
    predictions = tf.sigmoid(logits)

# Get the class predictions for labels.
predictions_classes = tf.round(predictions)

# Retrieve loss of network using cross entropy.
mean_xentropy = tf.reduce_mean(
    tf.nn.sigmoid_cross_entropy_with_logits(labels=labels, logits=logits))

# Define SGD optimizer with momentum and nesterov.
global_step = tf.Variable(0, dtype=tf.int32)

train_op = tf.train.MomentumOptimizer(
    learning_rate, momentum=momentum, use_nesterov=use_nesterov) \
        .minimize(loss=mean_xentropy, global_step=global_step)

# Metrics for finding best validation set.
tp, update_tp, reset_tp = metrics.create_reset_metric(
    metrics.true_positives, scope='tp', labels=y,
    predictions=predictions_classes, num_labels=num_labels)

fp, update_fp, reset_fp = metrics.create_reset_metric(
    metrics.false_positives, scope='fp', labels=y,
    predictions=predictions_classes, num_labels=num_labels)

fn, update_fn, reset_fn = metrics.create_reset_metric(
    metrics.false_negatives, scope='fn', labels=y,
    predictions=predictions_classes, num_labels=num_labels)

tn, update_tn, reset_tn = metrics.create_reset_metric(
    metrics.true_negatives, scope='tn', labels=y,
    predictions=predictions_classes, num_labels=num_labels)

confusion_matrix = metrics.confusion_matrix(
    tp, fp, fn, tn, num_labels=num_labels)

brier, update_brier, reset_brier = metrics.create_reset_metric(
    tf.metrics.mean_squared_error, scope='brier',
    labels=y, predictions=predictions)

auc, update_auc, reset_auc = metrics.create_reset_metric(
    tf.metrics.auc, scope='auc',
    labels=y, predictions=predictions)
tf.summary.scalar('auc', auc)

# Merge all the summaries and write them out.
summaries_op = tf.summary.merge_all()
train_writer = tf.summary.FileWriter(save_summaries_dir + "/train")
test_writer = tf.summary.FileWriter(save_summaries_dir + "/test")


def print_training_status(epoch, num_epochs, batch_num, xent, i_step=None):
    def length(x): return len(str(x))

    m = []
    m.append(
        f"Epoch: {{0:>{length(num_epochs)}}}/{{1:>{length(num_epochs)}}}"
        .format(epoch, num_epochs))
    m.append(f"Batch: {batch_num:>4}, Xent: {xent:6.4}")

    if i_step is not None:
        m.append(f"Step: {i_step:>10}")

    print(", ".join(m), end="\r")


def perform_test(init_op, summary_writer=None, epoch=None):
    tf.keras.backend.set_learning_phase(False)
    sess.run(init_op)

    # Reset all streaming variables.
    sess.run([reset_tp, reset_fp, reset_fn, reset_tn, reset_brier, reset_auc])

    try:
        while True:
            # Retrieve the validation set confusion metrics.
            sess.run([update_tp, update_fp, update_fn,
                      update_tn, update_brier, update_auc])

    except tf.errors.OutOfRangeError:
        pass

    # Retrieve confusion matrix and estimated roc auc score.
    test_conf_matrix, test_brier, test_auc, summaries = sess.run(
        [confusion_matrix, brier, auc, summaries_op])

    # Write summary.
    if summary_writer is not None:
        summary_writer.add_summary(summaries, epoch)

    # Print total roc auc score for validation.
    print(f"Brier score: {test_brier:6.4}, AUC: {test_auc:10.8}")

    # Print confusion matrix for each label.
    for i in range(num_labels):
        print(f"Confusion matrix for label {i+1}:")
        print(test_conf_matrix[i])


# Add ops for saving and restoring all variables.
saver = tf.train.Saver()

# Initialize variables.
sess.run(tf.global_variables_initializer())
sess.run(tf.local_variables_initializer())

# Train for the specified amount of epochs.
# Can be stopped early if peak of validation auc (Area under curve)
#  is reached.
latest_peak_auc = 0
waited_epochs = 0

for epoch in range(num_epochs):
    # Start training.
    tf.keras.backend.set_learning_phase(True)
    sess.run(train_init_op)
    batch_num = 0

    try:
        while True:
            # Optimize cross entropy.
            i_global, batch_xent, _ = sess.run(
                [global_step, mean_xentropy, train_op])

            # Print a nice training status.
            print_training_status(
                epoch, num_epochs, batch_num, batch_xent, i_global)

            # Report summaries.
            batch_num += 1

    except tf.errors.OutOfRangeError:
        print(f"\nEnd of epoch {epoch}!")

    perform_test(init_op=val_init_op, summary_writer=train_writer,
                 epoch=epoch)

    if val_auc < latest_peak_auc:
        # Stop early if peak of val auc has been reached.
        # If it is lower than the previous auc value, wait up to `wait_epochs`
        #  to see if it does not increase again.

        if wait_epochs == waited_epochs:
            print("Stopped early at epoch {0} with saved peak auc {1:10.8}"
                  .format(epoch+1, latest_peak_auc))
            break

        waited_epochs += 1
    else:
        latest_peak_auc = val_auc
        print(f"New peak auc reached: {val_auc:10.8}")

        # Save the model weights.
        saver.save(sess, save_model_path)

        # Reset waited epochs.
        waited_epochs = 0

# Close the session.
sess.close()
sys.exit(0)