import argparse
import os
import sys
from math import sqrt

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
from torch.autograd import Variable

if __name__ == '__main__' and __package__ is None:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
    __package__ = "pytorch_unet.trainer"

from pytorch_unet.utils.helpers import load_model, load_image

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


def parse_args(args):
    parser = argparse.ArgumentParser(description='Script for interpreting the trained model results')

    parser.add_argument('--main_dir', default="C:\\Users\\Mukesh\\Segmentation\\radnet\\", help='main directory')
    parser.add_argument('--interpret_path', default='./visualize', type=str,
                        help='Choose directory to save layer visualizations')
    parser.add_argument('--weights_dir', default="./weights", type=str, help='Choose directory to load weights from')
    parser.add_argument('--image_size', default=64, type=int, help='resize image size')
    parser.add_argument('--depth', default=3, type=int, help='Number of downsampling/upsampling blocks')
    parser.add_argument('--plot_interpret', default='block_filters', choices=['sensitivity', 'block_filters'], type=str,
                        help='Type of interpret to plot')
    parser.add_argument('--plot_size', default=128, type=int, help='Image size of sensitivity analysis')

    return parser.parse_args(args)


def all_children(mod):
    """Return a list of all child modules of the model, and their children, and their children's children, ..."""
    children = []
    for idx, child in enumerate(mod.named_modules()):
        children.append(child)
    return children


def get_values(iterables, key_to_find):
    """Get values for the child list."""
    return list(filter(lambda z: key_to_find in z, iterables))


def do_pooling(dep, d):
    """The named_modules isn't storing the pooling layers so this is a function to add pooling when plotting filers."""
    if (dep - 1) == 1:
        return F.avg_pool2d(d, 2)
    else:
        return F.avg_pool2d(do_pooling(dep - 1, d), 2)


def get_block_list(child_list, args):
    """Function to get the block list.
    Note:
        Depending on the depth of the unet and based on how the module path is saved, you go into the blocks and fetch
        down and up path blocks and append it into a list. The '[x][0][1]' is because of how the named_modules is
        returned.
    """

    down_block_list = []
    up_block_list = []
    down_seq = []
    up_seq = []

    for y in range(args.depth):
        down_seq.append(get_values(child_list, 'down_path.{}'.format(y)))

    for x in range(args.depth):
        down_block_list.append(down_seq[x][0][1])

    for y in range(args.depth - 1):
        up_seq.append(get_values(child_list, 'up_path.{}'.format(y)))

    for x in range(args.depth - 1):
        up_block_list.append(up_seq[x][0][1])

    return down_block_list, up_block_list


def plot_block(args, block, img_size, name):
    """Function to save the filter plots."""

    fig = plt.figure()
    plt.rcParams["figure.figsize"] = (50, 50)

    for i in range(img_size):
        fig.add_subplot(round(sqrt(img_size)) + 1, round(sqrt(img_size)) + 1, i + 1)
        plt.imshow(block[0][i].cpu().detach().numpy())
        plt.axis('off')
    fig.suptitle('{0} Block Filter Size {1}x{1}'.format(name, img_size), fontsize=25)
    plt.savefig(args.interpret_path + '/{}_block_filter_{}.png'.format(name, img_size))


def block_filters(model, img_path, args):
    """Function that activates block filer visualization.
    Note:
        You first take the module list of all the children and using the get_block_list function you separate the list
        into down and up list and then you go into the down list and get individual blocks and append into the depth
        list. Then using plot_block you plot individual filters iteratively. For up blocks, I call pooling for the
        down blocks to be able to center crop it and concatenate it to be able to upsample. This is done because of
        how the forward block is written in the unet upsampling module.
    """

    module_list = all_children(model)
    img_tensor, _ = load_image(img_path, args)
    input_img = img_tensor.unsqueeze(0).to(device)

    down_list, up_list = get_block_list(module_list, args)

    dep_list = [down_list[0](input_img)]
    for i in range(1, len(down_list)):
        layer = down_list[i](dep_list[i - 1])
        dep_list.append(layer)

    # call plotting for down block
    for j, m in zip(range(len(dep_list)), [64, 128, 256]):
        plot_block(args, dep_list[j], m, name='down')

    # apply pooling to everything inside dep list cause module list doesn't have pooling layers in it
    pooled = []
    for i in range(0, len(dep_list)):
        x = F.avg_pool2d(dep_list[i], 2)
        pooled.append(x)

    up_block = []
    reversed_uplist = up_list[::-1]
    for i in range(len(up_list)):
        pools = do_pooling(args.depth, dep_list[i + 1])
        up_layer = reversed_uplist[i](pools, F.avg_pool2d(dep_list[i], 2))
        up_block.append(up_layer)

    # call plotting for down block
    for j, m in zip(range(len(up_list)), [16, 32]):
        plot_block(args, up_block[j], m, name='up')


def sensitivity_analysis(model, image_tensor, target_class=None, postprocess='abs'):
    """Sensitivity analysis function.
    Note:
        Code is based on "https://github.com/jrieke/cnn-interpretability/blob/master/interpretation.py".
        Perform sensitivity analysis (via backpropagation; Simonyan et al. 2014) to determine the relevance of
        each image pixel for the classification decision. Return a relevance heatmap over the input image.
        You start by converting the image to tensor and then add a dimension to simulate the batch. Then you move
        the model to cpu and start eval mode and send in the image tensor through the model and get the output
        class pixel by pixel. Next we zero grad the model because pytorch accumulates the gradients on subsequent
        backward passes and one hot encode the output and using pytorch .backward you calculate the sum of gradients of
        a given tensor (one hot output) w.r.t the graph leaves. Finally using the .grad function you calculate the
        gradient of the input variable w.r.t the gradient found through backward pass, thereby comparing each.
    """

    image_tensor = torch.Tensor(image_tensor)
    X = Variable(image_tensor[None], requires_grad=True)
    model = model.cpu()
    model.eval()
    output = model(X)
    output_class = output.max(1)[1].data.numpy()[0]
    # print('Image was classified as:', output_class)

    model.zero_grad()
    one_hot_output = torch.zeros(output.size())
    if target_class is None:
        one_hot_output[0, output_class] = 1
    else:
        one_hot_output[0, target_class] = 1
    output.backward(gradient=one_hot_output)

    relevance_map = X.grad.data[0].numpy()

    if postprocess == 'abs':  # as in Simonyan et al. (2013)
        return np.abs(relevance_map)
    elif postprocess == 'square':  # as in Montavon et al. (2018)
        return relevance_map ** 2
    elif postprocess is None:
        return relevance_map
    else:
        raise ValueError()


def plot_sensitivity(img_path, model, args):
    """Function to save the sensitivity plot."""

    img_tensor, img = load_image(img_path, args)

    _mapped = sensitivity_analysis(model, img_tensor)
    mapped = np.squeeze(_mapped, axis=0)

    f, (ax1, ax2) = plt.subplots(1, 2)
    ax1.set_title('Input Image')
    ax1.axis('off')
    ax1.imshow(img)

    ax2.set_title('Sensitivity Map')
    ax2.axis('off')
    ax2.imshow(mapped, cmap='gist_gray')

    plt.savefig(args.interpret_path + '/sensitivity.png')


def main(args=None):
    """Contains the main function to start the interpretation.
    Note:
        The data is in volume format hence for demo purposes I only took one image to interpret so if this code is
        applied to another dataset that needs to be changed. You can change it by simply going to load_image in the
        helpers.py and removing the index[1]. When I start working on different datasets, I will modify this
        accordingly.
    """

    if args is None:
        args = sys.argv[1:]
    args = parse_args(args)

    if not os.path.exists(args.interpret_path):
        os.makedirs(args.interpret_path)

    model = load_model(args)
    img_path = os.path.join(args.main_dir, 'data', 'test-volume.tif')

    if args.plot_interpret == 'sensitivity':
        plot_sensitivity(img_path, model, args)

    if args.plot_interpret == 'block_filters':
        block_filters(model, img_path, args)

    print("Interpretation complete")


if __name__ == '__main__':
    main()
