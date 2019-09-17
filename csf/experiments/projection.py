"""
Code for projecting representations into a lower-dimensional space with PCA and t-SNE,
and plotting the results.
"""

import os

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
import tensorflow as tf
from absl import flags, logging
from pandas import DataFrame
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE

from csf import global_flags  # noqa
from csf.experiments.data import (
    N_OSM_SAMPLES,
    OSM_CLASSES,
    OSM_TILESIZE,
    load_osm_dataset,
)
from csf.experiments.utils import encoder_head

FLAGS = flags.FLAGS

# Required parameters
flags.DEFINE_string(
    "osm_data_prefix", None, "Glob matching the prefix of OSM data to use."
)
flags.DEFINE_list("experiment_bands", None, "Bands used for creating representations.")
flags.DEFINE_integer("perplexity", None, "Perplexity used for t-SNE.")
flags.mark_flags_as_required(["osm_data_prefix", "experiment_bands", "perplexity"])
flags.DEFINE_string(
    "model_checkpoint",
    None,
    "Path to a checkpoint used to initialize the encoder with. "
    "Must be provided if representations have not been created already.",
)

# Optional parameters with sensible defaults
flags.DEFINE_bool("plot", True, "Whether or not to save plots.")
flags.DEFINE_string(
    "representation_layer", "conv5_block3_out", "Which layer of representation to plot."
)
flags.DEFINE_integer(
    "n_points", 1024, "Number of points to plot.", upper_bound=N_OSM_SAMPLES
)
flags.DEFINE_string("out_dir", "visualizations", "Directory to save outputs.")
flags.DEFINE_integer("batch_size", 32, "Batch size for encoding step.")
flags.DEFINE_integer(
    "pca_preprocess_dims",
    200,
    "Number of dimensions to PCA the representation space before applying t-SNE."
    "If -1, do not apply PCA before t-SNE.",
)
flags.DEFINE_integer("tsne_iterations", 2000, "Maximum number of iterations for t-SNE.")


def _scatterplot(projection, labels, title, path):
    """
    Make a scatterplot of some projection of the input representations.

    Parameters
    ----------
    projection : ndarray
        An ndarray with shape [n, 2] containing the projection to plot.
    labels : [string]
        A word label for each datapoint.
    path : string
        Path to save the plot to.
    """
    plt.figure(figsize=(10, 10))
    sns.set(style="dark", palette="Paired")
    sns.scatterplot(
        data=DataFrame(
            {"x": projection[:, 0], "y": projection[:, 1], "OSM Label": labels}
        ),
        x="x",
        y="y",
        hue="OSM Label",
    )
    plt.tick_params(axis="x", which="both", bottom=False, top=False, labelbottom=False)
    plt.tick_params(axis="y", which="both", left=False, right=False, labelleft=False)
    plt.xlabel(None)
    plt.ylabel(None)
    plt.title(title)
    plt.savefig(path)
    plt.clf()


def make_projection_figures():
    """
    Make plots of the representations of various points from the OSM dataset.
    """
    logging.info("Making projection figures with flags:")
    logging.info(FLAGS.flags_into_string())

    # NOTE: number of points plotted is rounded down to a multiple of batch_size
    n_batches = FLAGS.n_points // FLAGS.batch_size
    n_points = n_batches * FLAGS.batch_size
    n_bands = len(FLAGS.experiment_bands)
    band_indices = [FLAGS.bands.index(band) for band in FLAGS.experiment_bands]

    if not os.path.exists(FLAGS.out_dir):
        logging.info("Creating path: {}".format(FLAGS.out_dir))
        os.makedirs(FLAGS.out_dir)

    logging.debug("Loading dataset.")
    dataset = (
        load_osm_dataset(FLAGS.osm_data_prefix, band_indices)
        .batch(FLAGS.batch_size)
        .take(n_batches)
    )

    representation_savepath = os.path.join(
        FLAGS.out_dir, "representations_{}.npy".format(FLAGS.representation_layer)
    )

    if not os.path.exists(representation_savepath):
        if not FLAGS.model_checkpoint:
            raise ValueError(
                "If representations have not been saved already, "
                "`model_checkpoint` must be provided."
            )

        logging.info("Writing new representations to {}".format(FLAGS.out_dir))
        logging.debug("Loading encoder.")
        encoder_inputs, _, encoder_representations = encoder_head(
            OSM_TILESIZE,
            FLAGS.experiment_bands,
            FLAGS.batch_size,
            FLAGS.model_checkpoint,
        )
        encoder = tf.keras.Model(
            inputs=encoder_inputs,
            outputs=[encoder_representations[FLAGS.representation_layer]],
        )
        representation_size = np.product(encoder.output.shape.as_list()[1:])

        @tf.function
        def process_batch(batch):
            images, labels_onehot = batch
            images_scaled = tf.cast((images + 1.0) * 128.0, tf.dtypes.uint8)
            labels = tf.cast(tf.argmax(labels_onehot, axis=1), tf.dtypes.uint8)
            representations = tf.reshape(encoder(images), (FLAGS.batch_size, -1))

            return images_scaled, labels, representations

        logging.debug("Initializing result storage.")
        images = np.zeros((n_points, OSM_TILESIZE, OSM_TILESIZE, n_bands), np.uint8)
        labels = np.zeros((n_points,), np.uint8)
        representations = np.zeros((n_points, representation_size), np.float32)

        logging.info("Encoding images.")
        for i, batch in dataset.enumerate():
            start_idx = i * FLAGS.batch_size
            end_idx = start_idx + FLAGS.batch_size
            images_, labels_, representations_ = process_batch(batch)

            images[start_idx:end_idx, :, :, :] = images_.numpy()
            labels[start_idx:end_idx] = labels_.numpy()
            representations[start_idx:end_idx, :] = representations_.numpy()

        logging.info("Done encoding!")
        np.save(os.path.join(FLAGS.out_dir, "images.npy"), images)
        np.save(os.path.join(FLAGS.out_dir, "labels.npy"), labels)
        np.save(representation_savepath, representations)

        if not FLAGS.plot:
            logging.info("Done!")
            return
    else:
        logging.info("Loading ndarrays from path: {}".format(FLAGS.out_dir))
        images = np.load(os.path.join(FLAGS.out_dir, "images.npy"))
        labels = np.load(os.path.join(FLAGS.out_dir, "labels.npy"))
        representations = np.load(representation_savepath)

    label_words = [OSM_CLASSES[label] for label in labels]

    logging.info("Running PCA.")
    pca_projection = PCA(n_components=2).fit_transform(representations)
    _scatterplot(
        pca_projection,
        label_words,
        "PCA of Representations",
        os.path.join(FLAGS.out_dir, "osm_pca.png"),
    )

    logging.info("Running pre-t-SNE PCA.")
    if FLAGS.pca_preprocess_dims:
        representations = PCA(n_components=FLAGS.pca_preprocess_dims).fit_transform(
            representations
        )

    logging.info("Running t-SNE.")
    tsne_projection = TSNE(
        n_components=2, perplexity=FLAGS.perplexity, n_iter=FLAGS.tsne_iterations
    ).fit_transform(representations)
    _scatterplot(
        tsne_projection,
        label_words,
        "t-SNE of Representations",
        os.path.join(FLAGS.out_dir, "osm_tsne.png"),
    )

    logging.info("Done!")
    logging.info("Results saved at: {}".format(FLAGS.out_dir))
