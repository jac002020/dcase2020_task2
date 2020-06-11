import os
import torch.utils.data
import glob
from dcase2020_task2.data_sets import BaseDataSet, CLASS_MAP, INVERSE_CLASS_MAP, TRAINING_ID_MAP, EVALUATION_ID_MAP, ALL_ID_MAP,\
    enumerate_development_datasets, enumerate_evaluation_datasets
import librosa
import numpy as np


class MCMDataSet(BaseDataSet):

    def __init__(
            self,
            machine_type,
            machine_id,
            data_root=os.path.join(os.path.expanduser('~'), 'shared', 'dcase2020_task2'),
            context=5,
            num_mel=128,
            n_fft=1024,
            hop_size=512,
            power=1.0,
            fmin=0,
            normalize_raw=False,
            hop_all=False
    ):
        self.data_root = data_root
        self.context = context
        self.num_mel = num_mel
        self.n_fft = n_fft
        self.hop_size = hop_size
        self.power = power
        self.fmin = fmin
        self.hop_all = hop_all

        assert type(machine_type) == int and type(machine_id) == int

        kwargs = {
            'data_root': self.data_root,
            'context': self.context,
            'num_mel': self.num_mel,
            'n_fft': self.n_fft,
            'hop_size': self.hop_size,
            'power': power,
            'normalize': normalize_raw,
            'fmin': fmin,
            'hop_all': hop_all
        }

        if machine_id == -1:
            training_sets = []
            validation_sets = []
            data = []
            for id_ in ALL_ID_MAP[machine_type]:
                training_sets.append(MachineDataSet(machine_type, id_, mode='training', **kwargs))
                validation_sets.append(MachineDataSet(machine_type, id_, mode='validation', **kwargs))
                data.append(training_sets[-1].data)

            data = np.concatenate(data, axis=-1)
            mean = data.mean(axis=1, keepdims=True)
            std = data.std(axis=1, keepdims=True)

            del data
            training_set = torch.utils.data.ConcatDataset(training_sets)
            validation_set = torch.utils.data.ConcatDataset(validation_sets)

        else:
            training_set = MachineDataSet(machine_type, machine_id, mode='training', **kwargs)
            validation_set = MachineDataSet(machine_type, machine_id, mode='validation', **kwargs)

            mean = training_set.data.mean(axis=1, keepdims=True)
            std = training_set.data.std(axis=1, keepdims=True)

        self.training_set = training_set
        self.validation_set = validation_set
        self.mean = mean
        self.std = std

    @property
    def observation_shape(self) -> tuple:
        return 1, self.num_mel, self.context

    def training_data_set(self):
        return self.training_set

    def validation_data_set(self):
        return self.validation_set

    def mean_std(self):
        return self.mean, self.std


class MachineDataSet(torch.utils.data.Dataset):

    def __init__(
            self,
            machine_type,
            machine_id,
            data_root=os.path.join(os.path.expanduser('~'), 'shared', 'dcase2020_task2'),
            mode='training',
            context=5,
            num_mel=128,
            n_fft=1024,
            hop_size=512,
            power=2.0,
            normalize=True,
            fmin=0,
            hop_all=False
    ):

        assert mode in ['training', 'validation']
        if mode == 'validation':
            hop_all = False

        self.num_mel = num_mel
        self.n_fft = n_fft
        self.hop_size = hop_size
        self.power = power
        self.normalize = normalize
        self.mode = mode
        self.data_root = data_root
        self.context = context
        self.machine_type = INVERSE_CLASS_MAP[machine_type]
        self.machine_id = machine_id
        self.fmin = fmin
        self.hop_all = hop_all

        if machine_id in TRAINING_ID_MAP[machine_type]:
            root_folder = 'dev_data'
        elif machine_id in EVALUATION_ID_MAP[machine_type]:
            root_folder = 'eval_data'
        else:
            raise AttributeError

        if mode == 'training':
            files = glob.glob(
                os.path.join(
                    data_root, root_folder, self.machine_type, 'train', '*id_{:02d}_*.wav'.format(machine_id)
                )
            )
        elif mode == 'validation':
            files = glob.glob(
                os.path.join(
                    data_root, root_folder, self.machine_type, 'test', '*id_{:02d}_*.wav'.format(machine_id)
                )
            )
        else:
            raise AttributeError

        assert len(files) > 0

        files = sorted(files)
        self.files = files
        self.file_length = self.__load_preprocess_file__(files[0]).shape[-1]
        self.num_samples_per_file = (self.file_length // self.context) if hop_all else (self.file_length - self.context + 1)
        self.meta_data = self.__load_meta_data__(files)
        self.data = self.__load_data__(files)

    def __getitem__(self, item):
        # get offset in audio file
        offset = item % self.num_samples_per_file
        # get audio file index
        item = item // self.num_samples_per_file
        # load audio file and extract audio junk
        offset = item * self.file_length + ((offset * self.context) if self.hop_all else offset)
        observation = self.data[:, offset:offset + self.context]
        # create data object
        meta_data = self.meta_data[item].copy()
        meta_data['observations'] = observation[None]

        return meta_data

    def __len__(self):
        return len(self.files) * self.num_samples_per_file

    def __load_meta_data__(self, files):
        data = []
        for f in files:
            md = self.__get_meta_data__(f)
            data.append(md)
        return data

    def __load_data__(self, files):
        file_name = "{}_{}_{}_{}_{}_{}_{}_{}_{}.npy".format(
            self.num_mel,
            self.n_fft,
            self.hop_size,
            self.power,
            self.mode,
            self.machine_type,
            self.machine_id,
            self.normalize,
            self.fmin
        )
        file_path = os.path.join(self.data_root, file_name)

        if os.path.exists(file_path):
            print('Loading {} data set for machine type {} id {}...'.format(self.mode, self.machine_type,
                                                                            self.machine_id))
            data = np.load(file_path)
        else:
            print('Loading & saving {} data set for machine type {} id {}...'.format(self.mode, self.machine_type,
                                                                                     self.machine_id))
            data = np.empty((self.num_mel, self.file_length * len(files)), dtype=np.float32)
            for i, f in enumerate(files):
                file = self.__load_preprocess_file__(f)
                if file.shape[1] != self.file_length:

                    if file.shape[1] < self.file_length:
                        print(f'Too short: {f}')
                        file = np.concatenate([
                            file,
                            file[:, :self.file_length - file.shape[1]]
                        ], -1)
                    elif file.shape[1] > self.file_length:
                        print(f'Too long: {f}')
                        file = file[:, :self.file_length]

                data[:, i * self.file_length:(i + 1) * self.file_length] = file

            np.save(file_path, data)

        return data

    def __load_preprocess_file__(self, file):
        x, sr = librosa.load(file, sr=None, mono=False)
        if self.normalize:
            x = (x - x.mean()) / x.std()

        x = librosa.feature.melspectrogram(
            y=x,
            sr=sr,
            n_fft=self.n_fft,
            hop_length=self.hop_size,
            n_mels=self.num_mel,
            power=self.power,
            fmin=self.fmin
        )

        if self.power == 1:
            x = librosa.core.amplitude_to_db(x)
        elif self.power == 2:
            x = librosa.core.power_to_db(x)
        else:
            raise AttributeError

        return x

    def __get_meta_data__(self, file_path):
        meta_data = os.path.split(file_path)[-1].split('_')
        machine_type = os.path.split(os.path.split(os.path.split(file_path)[0])[0])[1]
        machine_type = CLASS_MAP[machine_type]
        assert self.machine_type == INVERSE_CLASS_MAP[machine_type]
        if len(meta_data) == 4:
            if meta_data[0] == 'normal':
                y = 0
            elif meta_data[0] == 'anomaly':
                y = 1
            else:
                raise AttributeError
            assert self.machine_id == int(meta_data[2])
        elif len(meta_data) == 3:
            y = -1
            assert self.machine_id == int(meta_data[1])
        else:
            raise AttributeError

        return {
            'targets': y,
            'machine_types': machine_type,
            'machine_ids': self.machine_id,
            'file_ids': os.sep.join(os.path.normpath(file_path).split(os.sep)[-4:])
        }

if __name__ == '__main__':

    for type_, id_ in enumerate_development_datasets():
        _ = MachineDataSet(type_, id_, mode='training', n_fft=256)
        _ = MachineDataSet(type_, id_, mode='validation', n_fft=256)

    for type_, id_ in enumerate_evaluation_datasets():
        _ = MachineDataSet(type_, id_, mode='training', n_fft=256)
        _ = MachineDataSet(type_, id_, mode='validation', n_fft=256)


