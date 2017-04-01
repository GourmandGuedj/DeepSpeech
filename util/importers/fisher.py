from __future__ import absolute_import, division, print_function

import codecs
import fnmatch
import os
import pandas
import subprocess
import tensorflow as tf
import unicodedata
import wave

from glob import glob
from math import ceil
from six.moves import range
from threading import Thread
from util.audio import audiofile_to_input_vector
from util.data_set_helpers import DataSets
from util.gpu import get_available_gpus
from util.text import text_to_char_array, validate_label, ctc_label_dense_to_sparse

class DataSet(object):
    def __init__(self, filelist, thread_count, batch_size, numcep, numcontext, next_index=lambda x: x + 1):
        self._coord = None
        self._numcep = numcep
        self._x = tf.placeholder(tf.float32, [None, numcep + (2 * numcep * numcontext)])
        self._x_length = tf.placeholder(tf.int32, [])
        self._y = tf.placeholder(tf.int32, [None,])
        self._y_length = tf.placeholder(tf.int32, [])
        self.example_queue = tf.PaddingFIFOQueue(shapes=[[None, numcep + (2 * numcep * numcontext)], [], [None,], []],
                                                  dtypes=[tf.float32, tf.int32, tf.int32, tf.int32],
                                                  capacity=2 * self._get_device_count() * batch_size)
        self._enqueue_op = self.example_queue.enqueue([self._x, self._x_length, self._y, self._y_length])
        self._close_op = self.example_queue.close(cancel_pending_enqueues=True)
        self._filelist = filelist
        self.batch_size = batch_size
        self._numcontext = numcontext
        self._thread_count = thread_count
        self._files_list = self._create_files_list()
        self._next_index = next_index

    def _get_device_count(self):
        available_gpus = get_available_gpus()
        return max(len(available_gpus), 1)

    def start_queue_threads(self, session, coord):
        self._coord = coord
        batch_threads = [Thread(target=self._populate_batch_queue, args=(session,)) for i in range(self._thread_count)]
        for batch_thread in batch_threads:
            batch_thread.daemon = True
            batch_thread.start()
        return batch_threads

    def close_queue(self, session):
        session.run(self._close_op)

    def _create_files_list(self):
        # 1. Sort by wav filesize
        # 2. Select just wav filename and transcript columns
        # 3. Return a NumPy representation
        return self._filelist.sort_values(by="wav_filesize")        \
                             .ix[:, ["wav_filename", "transcript"]] \
                             .values

    def _indices(self):
        index = -1
        while not self._coord.should_stop():
            index = self._next_index(index) % len(self._files_list)
            yield self._files_list[index]

    def _populate_batch_queue(self, session):
        for wav_file, transcript in self._indices():
            source = audiofile_to_input_vector(wav_file, self._numcep, self._numcontext)
            source_len = len(source)
            target = text_to_char_array(transcript)
            target_len = len(target)
            try:
                session.run(self._enqueue_op, feed_dict={
                    self._x: source,
                    self._x_length: source_len,
                    self._y: target,
                    self._y_length: target_len})
            except tf.errors.CancelledError:
                return

    def next_batch(self):
        source, source_lengths, target, target_lengths = self.example_queue.dequeue_many(self.batch_size)
        sparse_labels = ctc_label_dense_to_sparse(target, target_lengths, self.batch_size)
        return source, source_lengths, sparse_labels

    @property
    def total_batches(self):
        # Note: If len(_filelist) % batch_size != 0, this re-uses initial files
        return int(ceil(len(self._filelist) / self.batch_size))


def read_data_sets(data_dir, train_csvs, dev_csvs, test_csvs,
                   train_batch_size, dev_batch_size, test_batch_size,
                   numcep, numcontext, thread_count=8,
                   stride=1, offset=0, next_index=lambda s, i: i + 1,
                   limit_dev=0, limit_test=0, limit_train=0, sets=[]):
    # Assume data_dir contains extracted LDC2004S13, LDC2004T19, LDC2005S13, LDC2005T19

    # Read the processed set files from disk if they exist, otherwise create them.
    def read_csvs(csvs):
        files = None
        for csv in csvs:
            file = pandas.read_csv(csv)
            if files is None:
                files = file
            else:
                files = files.append(file)
        return files

    train_files = read_csvs(train_csvs)
    dev_files = read_csvs(dev_csvs)
    test_files = read_csvs(test_csvs)

    if train_files is None or dev_files is None or test_files is None:
        # Conditionally convert Fisher sph data to wav
        _maybe_convert_wav(data_dir, "LDC2004S13", "fisher-2004-wav")
        _maybe_convert_wav(data_dir, "LDC2005S13", "fisher-2005-wav")

        # Conditionally split Fisher wav data
        _split_wav_and_sentences(data_dir,
                                 original_data="fisher-2004-wav",
                                 converted_data="fisher-2004-split-wav",
                                 trans_data=os.path.join("LDC2004T19", "fe_03_p1_tran", "data", "trans"))
        _split_wav_and_sentences(data_dir,
                                 original_data="fisher-2005-wav",
                                 converted_data="fisher-2005-split-wav",
                                 trans_data=os.path.join("LDC2005T19", "fe_03_p2_tran", "data", "trans"))

        # Conditionally split Fisher data into train/validation/test sets
        train_2004, dev_2004, test_2004 = _split_sets(data_dir, "fisher-2004-split-wav", "fisher-2004-split-wav-sets")
        train_2005, dev_2005, test_2005 = _split_sets(data_dir, "fisher-2005-split-wav", "fisher-2005-split-wav-sets")

        # The following file has an incorrect transcript that is much longer than
        # the audio source. The result is that we end up with more labels than time
        # slices, which breaks CTC. We fix this directly since it's a single occurrence
        # in the entire corpus.
        problematic_file = "fe_03_00265-33.53-33.81.wav"
        train_2004.loc[train_2004.loc[train_2004["wav_filename"] == problematic_file], "transcript"] = "correct"

        train_files = train_2004.append(train_2005)
        dev_files = dev_2004.append(dev_2005)
        dev_files = dev_2004.append(dev_2005)

        # Write sets to disk as CSV files
        train_files.to_csv(os.path.join(data_dir, "fisher-train.csv"), index=False)
        dev_files.to_csv(os.path.join(data_dir, "fisher-dev.csv"), index=False)
        test_files.to_csv(os.path.join(data_dir, "fisher-test.csv"), index=False)

    # Create train DataSet
    train = None
    if "train" in sets:
        train = _read_data_set(train_files, thread_count, train_batch_size, numcep, numcontext, stride=stride, offset=offset, next_index=lambda i: next_index('train', i), limit=limit_train)

    # Create dev DataSet
    dev = None
    if "dev" in sets:
        dev = _read_data_set(dev_files, thread_count, dev_batch_size, numcep, numcontext, stride=stride, offset=offset, next_index=lambda i: next_index('dev', i), limit=limit_dev)

    # Create test DataSet
    test = None
    if "test" in sets:
        test = _read_data_set(test_files, thread_count, test_batch_size, numcep, numcontext, stride=stride, offset=offset, next_index=lambda i: next_index('test', i), limit=limit_test)

    # Return DataSets
    return DataSets(train, dev, test)

def _maybe_convert_wav(data_dir, original_data, converted_data):
    source_dir = os.path.join(data_dir, original_data)
    target_dir = os.path.join(data_dir, converted_data)

    # Conditionally convert sph files to wav files
    if os.path.exists(target_dir):
        print("skipping maybe_convert_wav")
        return

    # Create target_dir
    os.makedirs(target_dir)

    # Loop over sph files in source_dir and convert each to 16-bit PCM wav
    for root, dirnames, filenames in os.walk(source_dir):
        for filename in fnmatch.filter(filenames, "*.sph"):
            sph_file = os.path.join(root, filename)
            for channel in ["1", "2"]:
                wav_filename = os.path.splitext(os.path.basename(sph_file))[0] + "_c" + channel + ".wav"
                wav_file = os.path.join(target_dir, wav_filename)
                print("converting {} to {}".format(sph_file, wav_file))
                subprocess.check_call(["sph2pipe", "-c", channel, "-p", "-f", "rif", sph_file, wav_file])

def _parse_transcriptions(trans_file):
    segments = []
    with codecs.open(trans_file, "r", "utf-8") as fin:
        for line in fin:
            if line.startswith("#") or len(line) <= 1:
                continue

            tokens = line.split()
            start_time = float(tokens[0])
            stop_time = float(tokens[1])
            speaker = tokens[2]
            transcript = " ".join(tokens[3:])

            transcript = unicodedata.normalize("NFKD", transcript)  \
                                    .encode("ascii", "ignore")      \
                                    .decode("ascii", "ignore")

            segments.append({
                "start_time": start_time,
                "stop_time": stop_time,
                "speaker": speaker,
                "transcript": transcript,
            })
    return segments

def _split_wav_and_sentences(data_dir, trans_data, original_data, converted_data):
    trans_dir = os.path.join(data_dir, trans_data)
    source_dir = os.path.join(data_dir, original_data)
    target_dir = os.path.join(data_dir, converted_data)

    files = []

    # Loop over transcription files and split corresponding wav
    for root, dirnames, filenames in os.walk(trans_dir):
        for filename in fnmatch.filter(filenames, "*.txt"):
            trans_file = os.path.join(root, filename)
            segments = _parse_transcriptions(trans_file)

            # Open wav corresponding to transcription file
            wav_filenames = [os.path.splitext(os.path.basename(trans_file))[0] + "_c" + channel + ".wav" for channel in ["1", "2"]]
            wav_files = [os.path.join(source_dir, wav_filename) for wav_filename in wav_filenames]

            print("splitting {} according to {}".format(wav_files, trans_file))

            origAudios = [wave.open(wav_file, "r") for wav_file in wav_files]

            # Loop over segments and split wav_file for each segment
            for segment in segments:
                # Create wav segment filename
                start_time = segment["start_time"]
                stop_time = segment["stop_time"]
                new_wav_filename = os.path.splitext(os.path.basename(trans_file))[0] + "-" + str(start_time) + "-" + str(stop_time) + ".wav"
                new_wav_file = os.path.join(target_dir, new_wav_filename)

                channel = 0 if segment["speaker"] == "A:" else 1
                _split_wav(origAudios[channel], start_time, stop_time, new_wav_file)

                new_wav_filesize = os.path.getsize(new_wav_file)
                transcript = validate_label(segment["transcript"])
                if transcript != None:
                    files.append((new_wav_file, new_wav_filesize, transcript))

            # Close origAudios
            for origAudio in origAudios:
                origAudio.close()

    return pandas.DataFrame(data=files, columns=["wav_filename", "wav_filesize", "transcript"])

def _split_wav(origAudio, start_time, stop_time, new_wav_file):
    frameRate = origAudio.getframerate()
    origAudio.setpos(int(start_time*frameRate))
    chunkData = origAudio.readframes(int((stop_time - start_time)*frameRate))
    chunkAudio = wave.open(new_wav_file, "w")
    chunkAudio.setnchannels(origAudio.getnchannels())
    chunkAudio.setsampwidth(origAudio.getsampwidth())
    chunkAudio.setframerate(frameRate)
    chunkAudio.writeframes(chunkData)
    chunkAudio.close()

def _split_sets(filelist):
    # We initially split the entire set into 80% train and 20% test, then
    # split the train set into 80% train and 20% validation.
    train_beg = 0
    train_end = int(0.8 * len(filelist))

    dev_beg = int(0.8 * train_end)
    dev_end = train_end
    train_end = dev_beg

    test_beg = dev_end
    test_end = len(filelist)

    return filelist[train_beg:train_end],
           filelist[dev_beg:dev_end],
           filelist[test_beg:test_end]

def _read_data_set(filelist, thread_count, batch_size, numcep, numcontext, stride=1, offset=0, next_index=lambda i: i + 1, limit=0):
    # Optionally apply dataset size limit
    if limit > 0:
        filelist = filelist.iloc[:limit]

    filelist = filelist[offset::stride]

    # Return DataSet
    return DataSet(txt_files, thread_count, batch_size, numcep, numcontext, next_index=next_index)
