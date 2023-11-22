from typing import Any, Dict, Iterable, List, Optional

import torch
from gluonts.core.component import validated
from gluonts.dataset.common import Dataset
from gluonts.dataset.field_names import FieldName
from gluonts.dataset.loader import as_stacked_batches
from gluonts.itertools import Cyclic
from gluonts.time_feature import TimeFeature, time_features_from_frequency_str
from gluonts.torch.distributions import DistributionOutput, StudentTOutput
from gluonts.torch.model.estimator import PyTorchLightningEstimator
from gluonts.torch.model.predictor import PyTorchPredictor
from gluonts.torch.modules.loss import DistributionLoss, NegativeLogLikelihood
from gluonts.transform import (
    AddAgeFeature,
    AddObservedValuesIndicator,
    # AddTimeFeatures,
    DummyValueImputation,
    AsNumpyArray,
    Chain,
    ExpectedNumInstanceSampler,
    InstanceSplitter,
    RemoveFields,
    SetField,
    TestSplitSampler,
    Transformation,
    ValidationSplitSampler,
    VstackFeatures,
)
from gluonts.transform.sampler import InstanceSampler

from lightning_module import NSTransformerLightningModule
from module import NSTransformerModel

# -

# PREDICTION_INPUT_NAMES = [
#     "feat_static_cat",
#     "feat_static_real",
#     "past_time_feat",
#     "past_target",
#     "past_observed_values",
#     "future_time_feat",
# ]
PREDICTION_INPUT_NAMES = ["past_target", "past_observed_values"]
TRAINING_INPUT_NAMES = PREDICTION_INPUT_NAMES + [
    "future_target",
    "future_observed_values",
]


class NSTransformerEstimator(PyTorchLightningEstimator):
    @validated()
    def __init__(
        self,
        prediction_length: int,
        # Transformer arguments
        nhead: int,
        num_encoder_layers: int,
        num_decoder_layers: int,
        dim_feedforward: int,
        # freq: Optional[str] = None,
        input_size: int = 1,
        activation: str = "gelu",
        dropout: float = 0.1,
        context_length: Optional[int] = None,
        # num_feat_dynamic_real: int = 0,
        # num_feat_static_cat: int = 0,
        # num_feat_static_real: int = 0,
        # cardinality: Optional[List[int]] = None,
        # embedding_dimension: Optional[List[int]] = None,
        distr_output: DistributionOutput = StudentTOutput(),
        loss: DistributionLoss = NegativeLogLikelihood(),
        # lags_seq: Optional[List[int]] = None,
        # time_features: Optional[List[TimeFeature]] = None,
        num_parallel_samples: int = 100,
        batch_size: int = 32,
        num_batches_per_epoch: int = 50,
        weight_decay: float = 1e-8,
        lr: float = 1e-3,
        aug_prob: float = 0.1,
        aug_rate: float = 0.1,
        trainer_kwargs: Optional[Dict[str, Any]] = dict(),
        train_sampler: Optional[InstanceSampler] = None,
        validation_sampler: Optional[InstanceSampler] = None,
        ckpt_path: Optional[str] = None,
    ) -> None:
        trainer_kwargs = {
            "max_epochs": 100,
            **trainer_kwargs,
        }
        super().__init__(trainer_kwargs=trainer_kwargs)

        # self.freq = freq
        self.context_length = (
            context_length if context_length is not None else prediction_length
        )
        self.prediction_length = prediction_length
        self.distr_output = distr_output
        self.loss = loss

        self.input_size = input_size
        self.nhead = nhead
        self.num_encoder_layers = num_encoder_layers
        self.num_decoder_layers = num_decoder_layers
        self.activation = activation
        self.dim_feedforward = dim_feedforward
        self.dropout = dropout
        self.lr = lr
        self.weight_decay = weight_decay
        self.aug_prob = aug_prob
        self.aug_rate = aug_rate
        # self.num_feat_dynamic_real = num_feat_dynamic_real
        # self.num_feat_static_cat = num_feat_static_cat
        # self.num_feat_static_real = num_feat_static_real
        # self.cardinality = (
        #     cardinality if cardinality and num_feat_static_cat > 0 else [1]
        # )
        # self.embedding_dimension = embedding_dimension
        # self.lags_seq = lags_seq
        # self.time_features = (
        #     time_features
        #     if time_features is not None
        #     else time_features_from_frequency_str(self.freq)
        # )

        self.num_parallel_samples = num_parallel_samples
        self.batch_size = batch_size
        self.num_batches_per_epoch = num_batches_per_epoch

        self.train_sampler = train_sampler or ExpectedNumInstanceSampler(
            num_instances=1.0, min_future=prediction_length
        )
        self.validation_sampler = validation_sampler or ValidationSplitSampler(
            min_future=prediction_length
        )
        self.ckpt_path = ckpt_path

    # def create_transformation(self) -> Transformation:
    #     remove_field_names = []
    #     if self.num_feat_static_real == 0:
    #         remove_field_names.append(FieldName.FEAT_STATIC_REAL)
    #     if self.num_feat_dynamic_real == 0:
    #         remove_field_names.append(FieldName.FEAT_DYNAMIC_REAL)

    #     return Chain(
    #         [RemoveFields(field_names=remove_field_names)]
    #         + (
    #             [SetField(output_field=FieldName.FEAT_STATIC_CAT, value=[0])]
    #             if not self.num_feat_static_cat > 0
    #             else []
    #         )
    #         + (
    #             [SetField(output_field=FieldName.FEAT_STATIC_REAL, value=[0.0])]
    #             if not self.num_feat_static_real > 0
    #             else []
    #         )
    #         + [
    #             AsNumpyArray(
    #                 field=FieldName.FEAT_STATIC_CAT,
    #                 expected_ndim=1,
    #                 dtype=int,
    #             ),
    #             AsNumpyArray(
    #                 field=FieldName.FEAT_STATIC_REAL,
    #                 expected_ndim=1,
    #             ),
    #             AsNumpyArray(
    #                 field=FieldName.TARGET,
    #                 # in the following line, we add 1 for the time dimension
    #                 expected_ndim=1 + len(self.distr_output.event_shape),
    #             ),
    #             AddObservedValuesIndicator(
    #                 target_field=FieldName.TARGET,
    #                 output_field=FieldName.OBSERVED_VALUES,
    #             ),
    #             AddTimeFeatures(
    #                 start_field=FieldName.START,
    #                 target_field=FieldName.TARGET,
    #                 output_field=FieldName.FEAT_TIME,
    #                 time_features=self.time_features,
    #                 pred_length=self.prediction_length,
    #             ),
    #             AddAgeFeature(
    #                 target_field=FieldName.TARGET,
    #                 output_field=FieldName.FEAT_AGE,
    #                 pred_length=self.prediction_length,
    #                 log_scale=True,
    #             ),
    #             VstackFeatures(
    #                 output_field=FieldName.FEAT_TIME,
    #                 input_fields=[FieldName.FEAT_TIME, FieldName.FEAT_AGE]
    #                 + (
    #                     [FieldName.FEAT_DYNAMIC_REAL]
    #                     if self.num_feat_dynamic_real > 0
    #                     else []
    #                 ),
    #             ),
    #         ]
    #     )

    def create_transformation(self) -> Transformation:
        return Chain(
            [
                AddObservedValuesIndicator(
                    target_field=FieldName.TARGET,
                    output_field=FieldName.OBSERVED_VALUES,
                    imputation_method=DummyValueImputation(0.0),
                ),
            ]
        )    
    
    def _create_instance_splitter(
        self, module: NSTransformerLightningModule, mode: str
    ):
        assert mode in ["training", "validation", "test"]

        instance_sampler = {
            "training": self.train_sampler,
            "validation": self.validation_sampler,
            "test": TestSplitSampler(),
        }[mode]

        return InstanceSplitter(
            target_field=FieldName.TARGET,
            is_pad_field=FieldName.IS_PAD,
            start_field=FieldName.START,
            forecast_start_field=FieldName.FORECAST_START,
            instance_sampler=instance_sampler,
            past_length=module.model._past_length,
            future_length=self.prediction_length,
            time_series_fields=[
                # FieldName.FEAT_TIME,
                FieldName.OBSERVED_VALUES,
            ],
            dummy_value=self.distr_output.value_in_support,
        )

    def create_training_data_loader(
        self,
        data: Dataset,
        module: NSTransformerLightningModule,
        shuffle_buffer_length: Optional[int] = None,
        **kwargs,
    ) -> Iterable:
        data = Cyclic(data).stream()
        instances = self._create_instance_splitter(module, "training").apply(
            data, is_train=True
        )
        return as_stacked_batches(
            instances,
            batch_size=self.batch_size,
            shuffle_buffer_length=shuffle_buffer_length,
            field_names=TRAINING_INPUT_NAMES,
            output_type=torch.tensor,
            num_batches_per_epoch=self.num_batches_per_epoch,
        )

    def create_validation_data_loader(
        self,
        data: Dataset,
        module: NSTransformerLightningModule,
        **kwargs,
    ) -> Iterable:
        instances = self._create_instance_splitter(module, "validation").apply(
            data, is_train=True
        )
        return as_stacked_batches(
            instances,
            batch_size=self.batch_size,
            field_names=TRAINING_INPUT_NAMES,
            output_type=torch.tensor,
        )

    def create_predictor(
        self,
        transformation: Transformation,
        module: NSTransformerLightningModule,
    ) -> PyTorchPredictor:
        prediction_splitter = self._create_instance_splitter(module, "test")

        return PyTorchPredictor(
            input_transform=transformation + prediction_splitter,
            input_names=PREDICTION_INPUT_NAMES,
            prediction_net=module.model,
            batch_size=self.batch_size,
            prediction_length=self.prediction_length,
            device=torch.device("cuda" if torch.cuda.is_available() else "cpu"),
        )

    def create_lightning_module(self) -> NSTransformerLightningModule:
        # model = NSTransformerModel(
        model_kwargs = {
            # freq=self.freq,
            "context_length":self.context_length,
            "prediction_length":self.prediction_length,
            # num_feat_dynamic_real=1
            # + self.num_feat_dynamic_real
            # + len(self.time_features),
            # num_feat_static_real=max(1, self.num_feat_static_real),
            # num_feat_static_cat=max(1, self.num_feat_static_cat),
            # cardinality=self.cardinality,
            # embedding_dimension=self.embedding_dimension,
            # transformer arguments
            "nhead":self.nhead,
            "num_encoder_layers":self.num_encoder_layers,
            "num_decoder_layers":self.num_decoder_layers,
            "activation":self.activation,
            "dropout":self.dropout,
            "dim_feedforward":self.dim_feedforward,
            # univariate input
            "input_size":self.input_size,
            "distr_output":self.distr_output,
            # lags_seq=self.lags_seq,
            "num_parallel_samples":self.num_parallel_samples,
        }

        # return NSTransformerLightningModule(model=model, loss=self.loss)
        if self.ckpt_path is not None:
            return NSTransformerLightningModule.load_from_checkpoint(
                self.ckpt_path,
                model_kwargs=model_kwargs,
                loss=self.loss,
                lr=self.lr,
                weight_decay=self.weight_decay,
                aug_prob=self.aug_prob,
                aug_rate=self.aug_rate,
            )
        else:
            return NSTransformerLightningModule(
                model_kwargs=model_kwargs,
                loss=self.loss,
                lr=self.lr,
                weight_decay=self.weight_decay,
                # context_length=self.context_length,
                # prediction_length=self.prediction_length,
                aug_prob=self.aug_prob,
                aug_rate=self.aug_rate,
            )

