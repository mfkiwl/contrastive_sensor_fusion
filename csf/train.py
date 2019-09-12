"""
Code for training models unsupervised using contrastive sensor fusion.
"""

import tensorflow as tf
from absl import flags, logging

import csf.data
import csf.global_flags as gf
import csf.utils
from csf.encoder import RESNET_REPRESENTATION_LAYERS, resnet_encoder

FLAGS = flags.FLAGS


# Required hyperparameters
flags.DEFINE_integer(
    "model_tilesize",
    None,
    "Tilesize model accepts for unsupervised learning. "
    "Views are asymmetrically cropped to this size from `data_tilesize` (see data.py).",
    lower_bound=1,
)
flags.DEFINE_float("learning_rate", None, "Learning rate for unsupervised training.")
flags.DEFINE_float("band_dropout_rate", None, "Final rate of dropping out bands.")
flags.DEFINE_list(
    "layer_loss_weights",
    None,
    "Weights for loss at various layers, as a comma-separated list of name:weight "
    "pairs like `conv4_block5_out:0.5`.",
)

# Optional hyperparameters, with sensible defaults.
# For best performance, do tune some of these.
flags.DEFINE_integer(
    "learning_rate_warmup_batches",
    None,
    "How many batches to warm up learning rate over. "
    "If unspecified, learning rate warmup is not used.",
)
flags.DEFINE_integer(
    "band_dropout_rate_warmup_batches",
    None,
    "How many batches to increase band dropout rate over. "
    "If unspecified, band dropout is constant.",
)
flags.DEFINE_float(
    "random_brightness_delta",
    None,
    "Maximum amount to randomize brightness between views.",
    lower_bound=0.0,
    upper_bound=1.0,
)
flags.DEFINE_float(
    "random_contrast_delta",
    None,
    "Maximum amount to randomize contrast between views.",
    lower_bound=0.0,
    upper_bound=1.0,
)
flags.DEFINE_float(
    "softmax_temperature",
    0.1,
    "Temperature to use for softmax loss. Changing this parameter is not recommended.",
    lower_bound=0.01,
)
flags.DEFINE_float(
    "gradient_clipnorm",
    1.0,
    "Clip gradients with norm above this value. "
    "Changing this parameter is not recommended.",
)

# Non-model training settings
flags.DEFINE_string(
    "out_dir", None, "Path used to store the outputs of unsupervised training."
)
flags.DEFINE_integer(
    "train_batches", None, "Number of batches of unsupervised training to carry out."
)
flags.DEFINE_string(
    "initial_checkpoint",
    None,
    "Path to a checkpoint used to continue unsupervised training from."
    "If unspecified, initialize model weights randomly.",
)
flags.DEFINE_integer(
    "summary_frequency",
    100,
    "How many batches to wait between saving summaries. If 0, do not save summaries. "
    "Note that summary steps require additional CPU-accelerator communication and may "
    "slow down training if used too often.",
)
flags.DEFINE_integer(
    "checkpoint_frequency", 500, "How many batches to wait between saving checkpoints."
)
flags.DEFINE_integer("max_checkpoints", 100, "The maximum number of checkpoints kept.")
flags.DEFINE_integer(
    "keep_checkpoint_every_n_hours",
    None,
    "Every `n` hours, marks a checkpoint to be kept permanently. "
    "If left unspecified, disables this behavior.",
)
flags.DEFINE_list(
    "visualize_bands",
    None,
    "Bands to visualize during training. Should be grouped into sets of 3. "
    "If left unspecified, do not visualize input imagery.",
)
flags.mark_flags_as_required(
    [
        "model_tilesize",
        "learning_rate",
        "band_dropout_rate",
        "layer_loss_weights",
        "train_batches",
        "out_dir",
    ]
)


@flags.validator("layer_loss_weights")
def _check_layer_loss_weights(values):
    try:
        _parse_layer_loss_weights(values)
    except Exception:
        return False
    return True


def _parse_layer_loss_weights(values):
    """
    Internal function used to access and validate layer loss weights.
    Do not use directly.
    """
    result = {}
    for value in values:
        name, weight = value.split(":")
        assert name in RESNET_REPRESENTATION_LAYERS
        result[name] = float(weight)
    return result


def layer_loss_weights():
    return _parse_layer_loss_weights(FLAGS.layer_loss_weights)


def input_shape():
    return (FLAGS.batch_size, FLAGS.model_tilesize, FLAGS.model_tilesize, gf.n_bands())


def _dropout_rate(step):
    with tf.name_scope("schedule_band_dropout_rate"):
        return csf.utils.optional_warmup(
            step, FLAGS.band_dropout_rate, FLAGS.band_dropout_rate_warmup_batches
        )


def _learning_rate(step):
    with tf.name_scope("schedule_learning_rate"):
        return csf.utils.optional_warmup(
            step, FLAGS.learning_rate, FLAGS.learning_rate_warmup_batches
        )


@tf.function
def _create_view(scene, step, dropout_rate, seed=None):
    """
    Apply augmentation to a set of input imagery, creating a new view.
    Note that this function is autograph-traced and takes a Python integer input (seed),
    so keep the number of calls with distinct seeds to a minimum.
    Do not pass Python values to any other argument.

    Parameters
    ----------
    scene : tf.Tensor
        A tensor of aligned input imagery.
    step : tf.Tensor
        A scalar, integer Tensor holding the current step.
    dropout_rate : tf.Tensor
        A scalar, float Tensor holding the current dropout rate.
        Included as an argument to work well with scheduling and autograph.
    seed : int, optional
        Random seed to use. Used to ensure that views get different random numbers.

    Returns
    -------
    tf.Tensor
        A view of the input imagery with crop, band dropout, and jitter applied.
    """
    seed = seed or FLAGS.random_seed

    if FLAGS.model_tilesize != FLAGS.data_tilesize:
        scene = tf.image.random_crop(scene, input_shape(), name="crop", seed=seed)

    if FLAGS.random_brightness_delta:
        scene = tf.image.random_brightness(
            scene, FLAGS.random_brightness_delta, seed=seed
        )
    if FLAGS.random_contrast_delta:
        scene = tf.image.random_contrast(
            scene,
            1.0 - FLAGS.random_contrast_delta,
            1.0 + FLAGS.random_contrast_delta,
            seed=seed,
        )

    scene = tf.nn.dropout(
        scene,
        dropout_rate,
        noise_shape=(FLAGS.batch_size, 1, 1, gf.n_bands()),
        name="band_dropout",
        seed=seed,
    )

    return scene


def _contrastive_loss(representation_1, representation_2, show_summaries=False):
    """
    Compute the contrastive loss for a pair of representations.
    Note that this function is autograph-traced and takes a Python boolean input
    (show_summaries), so two different graphs will be created.
    Do not pass Python values to any other argument.

    Parameters
    ----------
    representation_1 : tf.Tensor
        The representations for view 1 over this batch.
    representation_2 : tf.Tensor
        The representations for view 2 over this batch.
    show_summaries : bool, optional
        If True, plot summaries.

    Returns
    -------
    (tf.Tensor, tf.Tensor)
        The total loss and accuracy over this batch.
    """
    flat_1 = tf.reshape(representation_1, (FLAGS.batch_size, -1))
    flat_2 = tf.reshape(representation_2, (FLAGS.batch_size, -1))

    # Element [i, j] is the dot-product similarity of the i-th representation of
    # view 1 and the j-th representation of view 2 for scenes (i, j) in the batch.
    # The diagonal contains the similarities of matching scenes, which explains
    # our use of `diag_part` below to get the normalized logits for matching scenes.
    similarities = tf.linalg.matmul(
        flat_1, flat_2, transpose_b=True, name="similarities"
    )

    with tf.name_scope("similarity_visualization"):
        similarities_normalized = tf.expand_dims(
            tf.expand_dims(similarities / tf.reduce_max(similarities), axis=0), axis=-1
        )

    # Rescale similarities to apply softmax temperature
    similarities = tf.divide(
        similarities, FLAGS.softmax_temperature, name="sharpened_similarities"
    )

    with tf.name_scope("forward"):  # Predict view 2 from view 1
        softmax = tf.nn.log_softmax(similarities, axis=1, name="log_probabilities")
        nce_loss_forward = tf.negative(
            tf.reduce_mean(tf.linalg.diag_part(softmax)), name="nce_loss_forward"
        )
        del softmax

    with tf.name_scope("backward"):  # Predict view 1 from view 2
        softmax = tf.nn.log_softmax(similarities, axis=0, name="log_probabilities")
        nce_loss_backward = tf.negative(
            tf.reduce_mean(tf.linalg.diag_part(softmax)), name="nce_loss_backward"
        )
        del softmax

    nce_loss_total = tf.add(nce_loss_forward, nce_loss_backward, name="nce_loss_total")

    with tf.name_scope("compute_accuracy"):
        # Ideal predictions mean the greatest logit for each view is paired
        # (i.e. the diagonal dominates each row and column).
        ideal_predictions = tf.range(
            0, FLAGS.batch_size, 1, dtype=tf.int64, name="ideal_predictions"
        )
        predictions = tf.argmax(similarities, name="predictions")
        correct_predictions = tf.cast(
            tf.equal(predictions, ideal_predictions), tf.dtypes.float32
        )
        batch_accuracy = tf.reduce_mean(correct_predictions)

    if show_summaries:
        tf.summary.histogram(
            "representation_histogram",
            flat_1,
            description="Histogram of representations of view 1.",
        )
        tf.summary.histogram(
            "similarities_histogram",
            similarities,
            description="Histogram of similarities between views for each pair of "
            "scenes in the batch.",
        )

        # NOTE: Workaround for https://github.com/tensorflow/tensorflow/issues/28007
        #       Remove the device scope as soon as that issue's fixed.
        with tf.device("cpu:0"):
            tf.summary.image(
                "similarities_matrix",
                similarities_normalized,
                description="Matrix of similarities between views for each pair of "
                "scenes in the batch.",
            )

    return nce_loss_total, batch_accuracy


def _visualize_batch(batch):
    if FLAGS.visualize_bands:
        # NOTE: Workaround for https://github.com/tensorflow/tensorflow/issues/28007
        #       Remove the device scope as soon as that issue's fixed.
        with tf.device("cpu:0"):
            names, triples = csf.utils.partition_imagery(
                (batch / 2.0) + 0.5, FLAGS.bands, FLAGS.visualize_bands
            )
            for name, triple in zip(names, triples):
                tf.summary.image(name, triple)


# TODO(Aidan): TPU integration
# TODO(Aidan): Solve gcloud permissions issue
def train_unsupervised():
    logging.info(
        "Starting unsupervised training with flags:\n{}".format(
            FLAGS.flags_into_string()
        )
    )

    tf.random.set_seed(FLAGS.random_seed)

    logging.debug("Building global objects.")
    summary_writer = tf.summary.create_file_writer(FLAGS.out_dir)

    # Precomputed and kept static for use in tf.function
    layers_and_weights = layer_loss_weights().items()

    # List of (loss, accuracy) metric pairs
    if FLAGS.summary_frequency:
        total_loss_metric = tf.metrics.Mean()
        layer_metrics = [
            (tf.metrics.Mean(), tf.metrics.Mean()) for _ in layers_and_weights
        ]

    def write_metrics():
        tf.summary.scalar(
            "loss",
            total_loss_metric.result(),
            description="The total batch contrastive loss, averaged over the last "
            "`summary_frequency` batches.",
        )
        total_loss_metric.reset_states()

        for (loss_metric, accuracy_metric), (name, _) in zip(
            layer_metrics, layers_and_weights
        ):
            with tf.name_scope(name):
                tf.summary.scalar(
                    "loss",
                    loss_metric.result(),
                    description="The batch contrastive loss at layer {}, averaged over"
                    "the last `summary_frequency` batches.".format(name),
                )
                tf.summary.scalar(
                    "accuracy",
                    accuracy_metric.result(),
                    description="The batch contrastive accuracy at layer {}, averaged "
                    "over the last `summary_frequency` batches.".format(name),
                )
                loss_metric.reset_states()
                accuracy_metric.reset_states()

    step = tf.Variable(
        -1,
        trainable=False,
        name="step",
        dtype=tf.dtypes.int64,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
    )
    tf.summary.experimental.set_step(step)

    learning_rate = tf.Variable(
        _learning_rate(step),
        trainable=False,
        name="learning_rate",
        dtype=tf.dtypes.float32,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
    )

    dropout_rate = tf.Variable(
        _dropout_rate(step),
        trainable=False,
        name="dropout_rate",
        dtype=tf.dtypes.float32,
        aggregation=tf.VariableAggregation.ONLY_FIRST_REPLICA,
    )

    logging.debug("Building model and optimizer.")
    encoder = resnet_encoder(gf.n_bands())
    optimizer = tf.optimizers.Adam(learning_rate, clipnorm=FLAGS.gradient_clipnorm)

    ckpt = tf.train.Checkpoint(
        step=step,
        encoder=encoder,
        optimizer=optimizer,
        learning_rate=learning_rate,
        dropout_rate=dropout_rate,
    )
    ckpt_manager = tf.train.CheckpointManager(
        ckpt, FLAGS.out_dir, FLAGS.max_checkpoints, FLAGS.keep_checkpoint_every_n_hours
    )
    last_ckpt = ckpt_manager.latest_checkpoint

    if last_ckpt is not None:
        logging.info("Continuing training from checkpoint: {}".format(last_ckpt))
    else:
        logging.info("Initializing encoder with random weights.")

    logging.debug("Loading dataset.")
    ds = csf.data.load_dataset()

    # NOTE: Autograph will create two graphs for `train_step`, one with
    #       `show_summaries=True` and one with `show_summaries=False`.
    #       Do not pass Python values to any other argument.
    @tf.function
    def train_step(batch, dropout_rate, show_summaries=False):
        with tf.name_scope("training_step"):
            with tf.name_scope("view_1"):
                view_1 = _create_view(batch, step, dropout_rate, seed=1)
                _visualize_batch(view_1)
            with tf.name_scope("view_2"):
                view_2 = _create_view(batch, step, dropout_rate, seed=2)
                _visualize_batch(view_2)

            losses = []

            with tf.GradientTape() as tape:
                representations_1 = encoder(view_1)
                representations_2 = encoder(view_2)

                for (loss_metric, accuracy_metric), (layer, weight) in zip(
                    layer_metrics, layers_and_weights
                ):
                    with tf.name_scope("layer_{}".format(layer)):
                        loss, accuracy = _contrastive_loss(
                            representations_1[layer],
                            representations_2[layer],
                            show_summaries,
                        )
                        losses.append(weight * loss)

                        if FLAGS.summary_frequency:
                            loss_metric.update_state([loss])
                            accuracy_metric.update_state([accuracy])

                loss_total = tf.reduce_sum(losses, name="loss_total")
                if FLAGS.summary_frequency:
                    total_loss_metric.update_state([loss_total])

            gradients = tape.gradient(loss_total, encoder.trainable_weights)
            optimizer.apply_gradients(zip(gradients, encoder.trainable_weights))

            return loss_total

    logging.info("Beginning unsupervised training.")

    for batch in ds:
        current_step = int(step.assign_add(1))

        # TODO(Aidan): determine if summaries must be disabled when using TPUs
        show_summaries = (
            FLAGS.summary_frequency and current_step % FLAGS.summary_frequency == 0
        )
        with summary_writer.as_default(), tf.summary.record_if(show_summaries):
            with tf.name_scope("input_imagery"):
                _visualize_batch(batch)

            # Update schedules
            if FLAGS.learning_rate_warmup_batches:
                learning_rate.assign(_learning_rate(step))
                tf.summary.scalar("learning_rate", learning_rate)
            if FLAGS.band_dropout_rate_warmup_batches:
                dropout_rate.assign(_dropout_rate(step))
                tf.summary.scalar("dropout_rate", dropout_rate)

            if current_step == 0:
                logging.debug("Running trace for first batch.")
                tf.summary.trace_on()

            train_step(batch, dropout_rate, show_summaries)

            if current_step == 0:
                tf.summary.trace_export("training_step", step=0)
                tf.summary.trace_off()
                summary_writer.flush()

            if current_step % FLAGS.checkpoint_frequency == 0:
                save_path = ckpt_manager.save()
                logging.info(
                    "Saving checkpoints for step {}: {}".format(current_step, save_path)
                )

            # Flush metrics every `summary_frequency` batches
            if show_summaries:
                write_metrics()

            if current_step >= FLAGS.train_batches:
                logging.info("Finished training at step {}.".format(current_step))
                break

    logging.info("Done with unsupervised training.")
    ckpt_manager.save()


# TODO(Aidan): move this to a separate script
if __name__ == "__main__":
    import sys

    FLAGS(sys.argv)
    train_unsupervised()