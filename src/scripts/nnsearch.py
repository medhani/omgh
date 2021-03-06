import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import settings
import utils
sys.path.append(settings.CAFFE_PYTHON_PATH)
from storage import datastore
from dataset import CUB_200_2011
from deep_extractor import CNN_Features_CAFFE_REFERENCE
from parts import Parts
import click
import numpy as np
import sklearn.neighbors
import sklearn.svm
import sklearn.metrics
from time import time
import caffe


@click.command()
@click.option('--storage_name', default='cache-cccftt')
@click.option('--layer', default='pool5')
@click.option('--model', default='cccftt')
@click.option('--iteration', type=click.INT, default=100000)
@click.option('--normalize_feat', type=click.BOOL, default=True)
@click.option('--n_neighbors', type=click.INT, default=1)
@click.option('--parts', type=click.Choice(['head', 'body']), multiple=True)
@click.option('--feat_layer', default='fc7')
@click.option('--add_noise', type=click.BOOL, default=False)
@click.option('--to_oracle', type=click.BOOL, default=True)
@click.option('--noise_std_c', type=click.FLOAT, default=5.)
@click.option('--noise_std_d', type=click.FLOAT, default=5.)
@click.option('--augment_training', type=click.BOOL, default=False)
@click.option('--augmentation_fold', type=click.INT, default=2)
@click.option('--augmentation_noise', type=click.FLOAT, default=5.0)
def main(storage_name, layer, model, iteration, normalize_feat, n_neighbors, parts, feat_layer, add_noise, to_oracle, noise_std_c, noise_std_d, augment_training, augmentation_fold, augmentation_noise):
    if len(parts) == 0:
        print 'no parts where needed'
        exit()

    name = '%s-%s' % (model, iteration)

    nn_storage_name = 'nn-parts'
    nn_storage = datastore(settings.storage(nn_storage_name))
    nn_storage.super_name = '%s_%s' % (storage_name, name)
    nn_storage.sub_name = layer
    nn_storage.instance_name = 'norm_%s.mat' % str(normalize_feat)
    nn_storage.instance_path = nn_storage.get_instance_path(nn_storage.super_name, nn_storage.sub_name, nn_storage.instance_name)

    cub = CUB_200_2011(settings.CUB_ROOT)

    safe = datastore(settings.storage(storage_name))
    safe.super_name = 'features'
    safe.sub_name = name

    instance_path = safe.get_instance_path(safe.super_name, safe.sub_name, 'feat_cache_%s' % layer)
    feat = safe.load_large_instance(instance_path, 4)

    # should we normalize the feats?
    if normalize_feat:
        # snippet from : http://stackoverflow.com/a/8904762/428321
        # I've went for l2 normalization.
        # row_sums = feat.sum(axis=1)
        row_norms = np.linalg.norm(feat, axis=1)
        new_feat = feat / row_norms[:, np.newaxis]
        feat = new_feat

    IDtrain, IDtest = cub.get_train_test_id()

    # the following line is not really a good idea. Only works for this dataset.
    Xtrain = feat[IDtrain-1, :]
    Xtest = feat[IDtest-1, :]

    print 'init load done'

    if not nn_storage.check_exists(nn_storage.instance_path):
        print 'calculating'
        # the actual NN search
        nn_model = sklearn.neighbors.NearestNeighbors(n_neighbors=n_neighbors, algorithm='ball_tree', metric='minkowski', p=2)
        tic = time()
        nn_model.fit(Xtrain)
        toc = time() - tic
        print 'fitted in: ', toc

        tic = time()
        NNS = nn_model.kneighbors(Xtest, 1, return_distance=False)
        toc = time() - tic
        print 'found in: ', toc
        nn_storage.save_instance(nn_storage.instance_path, NNS)
    else:
        # load the NNS
        NNS = nn_storage.load_instance(nn_storage.instance_path)
        print 'loaded'

    # convert (N, 1) to (N,)
    NNS = NNS.T[0]

    # transfer part locations
    all_parts_cub = cub.get_parts()
    estimated_test_parts = Parts()
    all_image_infos = cub.get_all_image_infos()
    bbox = cub.get_bbox()

    tic = time()
    # estimate test parts with NN part transfer
    for i in range(IDtest.shape[0]):
        test_id = IDtest[i]
        nn_id = IDtrain[NNS[i]]
        nn_parts = all_parts_cub.for_image(nn_id)

        test_bbox = bbox[test_id - 1]
        nn_bbox = bbox[nn_id - 1]

        estimated_parts = nn_parts.transfer(nn_bbox, test_bbox)
        estimated_parts.set_for(test_id)
        estimated_test_parts.appends(estimated_parts)

    toc = time() - tic
    print 'transfered in', toc

    # load data
    tic = time()
    features_storage_r = datastore(settings.storage('ccrft'))
    feature_extractor_r = CNN_Features_CAFFE_REFERENCE(features_storage_r, make_net=False)

    features_storage_c = datastore(settings.storage('cccft'))
    feature_extractor_c = CNN_Features_CAFFE_REFERENCE(features_storage_c, make_net=False)

    if 'head' in parts:
        features_storage_p_h = datastore(settings.storage('ccpheadft-100000'))
        feature_extractor_p_h = CNN_Features_CAFFE_REFERENCE(features_storage_p_h, make_net=False)

    if 'body' in parts:
        features_storage_p_b = datastore(settings.storage('ccpbodyft-100000'))
        feature_extractor_p_b = CNN_Features_CAFFE_REFERENCE(features_storage_p_b, make_net=False)

    Xtrain_r, ytrain_r, Xtest_r, ytest_r = cub.get_train_test(feature_extractor_r.extract_one)
    Xtrain_c, ytrain_c, Xtest_c, ytest_c = cub.get_train_test(feature_extractor_c.extract_one)

    if 'head' in parts:
        Xtrain_p_h, ytrain_p_h, Xtest_p_h, ytest_p_h = cub.get_train_test(feature_extractor_p_h.extract_one)
    if 'body' in parts:
        Xtrain_p_b, ytrain_p_b, Xtest_p_b, ytest_p_b = cub.get_train_test(feature_extractor_p_b.extract_one)

    toc = time() - tic
    print 'loaded data in', toc

    def compute_estimated_part_data(model_name, shape, IDS, part_names_to_filter, add_noise, noise_std_c, noise_std_d):
        net = caffe.Classifier(settings.model(model_name), settings.pretrained(model_name), mean=np.load(settings.ILSVRC_MEAN), channel_swap=(2, 1, 0), raw_scale=255)
        net.set_phase_test()
        net.set_mode_gpu()

        # compute estimated head data
        new_Xtest_part = np.zeros(shape)

        for i, t_id in enumerate(IDS):
            if to_oracle:
                t_parts = all_parts_cub.for_image(t_id)
            else:
                t_parts = estimated_test_parts.for_image(t_id)
            t_img_addr = all_image_infos[t_id]
            t_img = caffe.io.load_image(t_img_addr)
            t_parts_part = t_parts.filter_by_name(part_names_to_filter)
            t_img_part = t_parts_part.get_rect(t_img, add_noise=add_noise, noise_std_c=noise_std_c, noise_std_d=noise_std_d)
            try:
                net.predict([t_img_part], oversample=False)
            except Exception, e:
                print '------', t_id, '----------'
                print part_names_to_filter
                print t_img_addr
                print '------------'
                print t_img.shape
                print t_parts
                print '------------'
                print t_img_part.shape
                print t_parts_part
                raise e
            new_Xtest_part[i, :] = net.blobs[feat_layer].data[0].flatten()

        return new_Xtest_part

    # compute estimated head data
    tic = time()
    if 'head' in parts:
        Xtest_p_h = compute_estimated_part_data('ccpheadft-100000', Xtest_p_h.shape, IDtest, Parts.HEAD_PART_NAMES, add_noise, noise_std_c, noise_std_d)
    if 'body' in parts:
        Xtest_p_b = compute_estimated_part_data('ccpbodyft-100000', Xtest_p_b.shape, IDtest, Parts.BODY_PART_NAMES, add_noise, noise_std_c, noise_std_d)
    toc = time() - tic
    print 'feature calculation in', toc

    # make the final feature vector
    train_tuple = (Xtrain_r, Xtrain_c)
    test_tuple = (Xtest_r, Xtest_c)
    if 'head' in parts:
        train_tuple = train_tuple + (Xtrain_p_h,)
        test_tuple = test_tuple + (Xtest_p_h,)
    if 'body' in parts:
        train_tuple = train_tuple + (Xtrain_p_b,)
        test_tuple = test_tuple + (Xtest_p_b,)

    Xtrain = np.concatenate(train_tuple, axis=1)
    Xtest = np.concatenate(test_tuple, axis=1)
    ytrain = ytrain_r

    print Xtrain.shape, Xtest.shape

    # training augmentation
    if augment_training:
        Xtrain_heads = []
        Xtrain_bodies = []
        for fold in range(augmentation_fold):
            print 'augmentation_fold', fold
            if 'head' in parts:
                new_Xtrain_p_h = compute_estimated_part_data('ccpheadft-100000', Xtrain_p_h.shape, IDtrain, Parts.HEAD_PART_NAMES, add_noise=True, noise_std_c=augmentation_noise, noise_std_d=augmentation_noise)
                Xtrain_heads.append(new_Xtrain_p_h)
            if 'body' in parts:
                new_Xtrain_p_b = compute_estimated_part_data('ccpbodyft-100000', Xtrain_p_b.shape, IDtrain, Parts.BODY_PART_NAMES, add_noise=True, noise_std_c=augmentation_noise, noise_std_d=augmentation_noise)
                Xtrain_bodies.append(new_Xtrain_p_b)

        for fold in range(augmentation_fold):
            train_tuple = (Xtrain_r, Xtrain_c)
            if 'head' in parts:
                train_tuple = train_tuple + (Xtrain_heads[fold],)
            if 'body' in parts:
                train_tuple = train_tuple + (Xtrain_bodies[fold],)
            Xtrain = np.concatenate((Xtrain, np.concatenate(train_tuple, axis=1)), axis=0)
            ytrain = np.concatenate((ytrain, ytrain_r))

    print Xtrain.shape, Xtest.shape

    # do classification
    tic = time()
    model = sklearn.svm.LinearSVC(C=0.0001)
    model.fit(Xtrain, ytrain)
    predictions = model.predict(Xtest)
    toc = time() - tic

    print 'classified in', toc
    print '--------------------'
    print 'parts', parts
    print 'add_noise', add_noise, 'to_oracle', to_oracle
    print 'augment_training', augment_training, 'augmentation_noise', augmentation_noise
    print 'noises, c: %f, d: %f' % (noise_std_c, noise_std_d)
    print '--------------------'
    print 'accuracy', sklearn.metrics.accuracy_score(ytest_r, predictions), 'mean accuracy', utils.mean_accuracy(ytest_r, predictions)
    print '===================='

if __name__ == '__main__':
    main()
