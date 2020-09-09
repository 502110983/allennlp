import glob
import os
from os import PathLike
from typing import Any, Dict, Tuple, Union, Optional
import pickle
import json
import numpy as np

from overrides import overrides
import torch

from allennlp.common.file_utils import cached_path, json_lines_from_file, TensorCache
from allennlp.data.dataset_readers.dataset_reader import DatasetReader
from allennlp.data.fields import ArrayField, LabelField, ListField, MetadataField, TextField
from allennlp.data.image_loader import ImageLoader
from allennlp.data.instance import Instance
from allennlp.data.token_indexers import PretrainedTransformerIndexer
from allennlp.data.token_indexers import TokenIndexer
from allennlp.data.tokenizers import PretrainedTransformerTokenizer
from allennlp.data.tokenizers import Tokenizer
from allennlp.modules.vision.grid_embedder import GridEmbedder
from allennlp.modules.vision.region_detector import RegionDetector


def load_vqa_dataset(dataset_root, image_root, prefix):
    """
    Load a json in VQA's annotation format and convert to Vision and Language Dataset Dict.
    Args:
        question_file (str): full path to the VQA json question_file.
        image_root (str): the directory of the image or features.
    """

    question_path = os.path.join(dataset_root, 'annotations', 'v2_OpenEnded_mscoco_%s_questions.json' %prefix)
    questions = sorted(
        json.load(open(question_path))["questions"], key=lambda x: x["question_id"]
    )

    dataset_dicts = []
    if "test" not in prefix:
        ans_path = os.path.join(dataset_root, 'cache', '%s_target.pkl' %prefix)
        # load answer pickle file
        answers = pickle.load(open(ans_path, "rb"))
        answers = sorted(answers, key=lambda x: x["question_id"])

        for question, answer in zip(questions, answers):
            record = {}
            assert question['question_id'] == answer['question_id']
            record['question'] = question['question']
            record['question_id'] = question['question_id']
            record['labels'] = answer['labels']
            record['scores'] = answer['scores']
            record['file_name'] = os.path.join(image_root, prefix, 'COCO_%s_%012d.jpg'%(prefix,question['image_id']))
            dataset_dicts.append(record)
    else:
        for question in questions:
            record = {}
            record['question'] = question['question']
            record['question_id'] = question['question_id']
            record['file_name'] = os.path.join(image_root, prefix, 'COCO_%s_%012d.jpg'%(prefix,question['image_id']))
            dataset_dicts.append(record)

    return dataset_dicts


@DatasetReader.register("vqav2")
class VQAv2Reader(DatasetReader):
    """
    Parameters
    ----------
    image_dir: `str`
        Path to directory containing `png` image files.
    image_featurizer: `GridEmbedder`
        The backbone image processor (like a ResNet), whose output will be passed to the region
        detector for finding object boxes in the image.
    region_detector: `RegionDetector`
        For pulling out regions of the image (both coordinates and features) that will be used by
        downstream models.
    data_dir: `str`
        Path to directory containing text files for each dataset split. These files contain
        the sentences and metadata for each task instance.
    tokenizer: `Tokenizer`, optional
    token_indexers: `Dict[str, TokenIndexer]`
    lazy : `bool`, optional
        Whether to load data lazily. Passed to super class.
    """
    def __init__(
        self,
        image_dir: Union[str, PathLike],
        image_loader: ImageLoader,
        image_featurizer: GridEmbedder,
        region_detector: RegionDetector,
        *,
        feature_cache_dir: Optional[Union[str, PathLike]] = None,
        data_dir: Union[str, PathLike] = None,
        tokenizer: Tokenizer = None,
        token_indexers: Dict[str, TokenIndexer] = None,
        cuda_device: Optional[Union[int, torch.device]] = None,
        lazy: bool = False,
    ) -> None:
        super().__init__(lazy)

        if cuda_device is None:
            from torch import cuda
            if cuda.device_count() > 0:
                cuda_device = 0
            else:
                cuda_device = -1
        from allennlp.common.checks import check_for_gpu
        check_for_gpu(cuda_device)
        from allennlp.common.util import int_to_device
        self.cuda_device = int_to_device(cuda_device)

        self._image_dir = image_dir
        self._data_dir = data_dir

        # tokenizers and indexers
        if not tokenizer:
            tokenizer = PretrainedTransformerTokenizer("bert-base-uncased")
        self._tokenizer = tokenizer
        if token_indexers is None:
            token_indexers = {"tokens": PretrainedTransformerIndexer("bert-base-uncased")}
        self._token_indexers = token_indexers

        ans2label_path = os.path.join(data_dir, "cache", "trainval_ans2label.pkl")
        label2ans_path = os.path.join(data_dir, "cache", "trainval_label2ans.pkl")
        self.ans2label = pickle.load(open(ans2label_path, "rb"))
        self.label2ans = pickle.load(open(label2ans_path, "rb"))
        self.num_labels = len(self.ans2label)

        # image loading
        self.image_loader = image_loader
        self.image_featurizer = image_featurizer.to(self.cuda_device)
        self.region_detector = region_detector.to(self.cuda_device)

        # feature cache
        if feature_cache_dir is None:
            self._features_cache = {}
            self._coordinates_cache = {}
        else:
            os.makedirs(feature_cache_dir, exist_ok=True)
            self._features_cache = TensorCache(os.path.join(feature_cache_dir, "features"))
            self._coordinates_cache = TensorCache(os.path.join(feature_cache_dir, "coordinates"))

    @overrides
    def _read(self, split: str):
        """
        split can be train, val, test, trainval, minival.
        """
        if split == 'train':
            datasets = load_vqa_dataset(self._data_dir, self._image_dir, 'train2014')
        elif split == 'val': 
            datasets = load_vqa_dataset(self._data_dir, self._image_dir, 'val2014')
        elif split == 'test':
            datasets = load_vqa_dataset(self._data_dir, self._image_dir, 'test2015')
        elif split == 'trainval':
            datasets_train = load_vqa_dataset(self._data_dir, self._image_dir, 'train2014')
            datasets_val = load_vqa_dataset(self._data_dir, self._image_dir, 'val2014')
            datasets = datasets_train + datasets_val[:-3000]
        elif split == 'minival':
            datasets_val = load_vqa_dataset(self._data_dir, self._image_dir, 'val2014')
            datasets = datasets_val[-3000:]
        else:
            pass

        for instance_dict in datasets:
            instance = self.text_to_instance(instance_dict, split)
            if instance is not None:
                yield instance

    @overrides
    def text_to_instance(
        self,  # type: ignore
        instance_dict: Dict,
        split: str,
    ) -> Instance:
        tokenized_sentence = self._tokenizer.tokenize(instance_dict['question'])
        sentence_field = TextField(tokenized_sentence, self._token_indexers)

        # Load images
        to_compute = []
        image_path = instance_dict['file_name']
        name = os.path.basename(image_path)

        if name not in self._features_cache or name not in self.__coordinates_cache:
            to_compute.append(image_path)

        if len(to_compute) > 0:
            images, sizes = self.image_loader(to_compute)
            with torch.no_grad():
                images = images.to(self.cuda_device)
                sizes = sizes.to(self.cuda_device)
                featurized_images = self.image_featurizer(images)
                detector_results = self.region_detector(images, sizes, featurized_images)
                features = detector_results["features"]
                coordinates = detector_results["coordinates"]

            for index, path in enumerate(to_compute):
                self._features_cache[os.path.basename(image_path)] = features[index].cpu()
                self._coordinates_cache[os.path.basename(image_path)] = coordinates[index].cpu()

        features = self._features_cache[name]
        coords = self._coordinates_cache[name]

        target = torch.zeros(self.num_labels)
        if "test" not in split:
            if len(instance_dict["labels"]):
                labels = torch.from_numpy(np.array(instance_dict["labels"]))
                scores = torch.from_numpy(np.array(instance_dict["scores"], dtype=np.float32))
                if labels is not None:
                    target.scatter_(0, labels, scores)

        fields = {
            "box_features": ArrayField(features),
            "box_coordinates": ArrayField(coords),
            "sentence": sentence_field,
            "identifier": MetadataField(instance_dict['question_id']),
            "label": ArrayField(target),
        }

        return Instance(fields)