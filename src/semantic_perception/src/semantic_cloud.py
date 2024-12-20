#!/usr/bin/env python
"""
Take in an image (rgb or rgb-d)
Use CNN to do semantic segmantation

"""

from __future__ import division
from __future__ import print_function

import sys
import os
import rospy
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
import numpy as np
from sensor_msgs.msg import PointCloud2
#import message_filters
import time
from skimage.transform import resize
import cv2
import torch
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'include'))
#from color_pcl_generator import PointType, ColorPclGenerator
import segmentation_models_pytorch as smp




def color_map(N=256, normalized=False):
    """
    Return Color Map in PASCAL VOC format (rgb)
    \param N (int) number of classes
    \param normalized (bool) whether colors are normalized (float 0-1)
    \return (Nx3 numpy array) a color map
    """
    def bitget(byteval, idx):
        return ((byteval & (1 << idx)) != 0)
    dtype = 'float32' if normalized else 'uint8'
    cmap = np.zeros((N, 3), dtype=dtype)
    for i in range(N):
        r = g = b = 0
        c = i
        for j in range(8):
            r = r | (bitget(c, 0) << 7-j)
            g = g | (bitget(c, 1) << 7-j)
            b = b | (bitget(c, 2) << 7-j)
            c = c >> 3
        cmap[i] = np.array([r, g, b])
    cmap = cmap/255.0 if normalized else cmap
    return cmap

def decode_segmap(temp, n_classes, cmap):
    """
    Given an image of class predictions, produce an bgr8 image with class colors
    \param temp (2d numpy int array) input image with semantic classes (as integer)
    \param n_classes (int) number of classes
    \cmap (Nx3 numpy array) input color map
    \return (numpy array bgr8) the decoded image with class colors
    """
    r = temp.copy()
    g = temp.copy()
    b = temp.copy()
    for l in range(0, n_classes):
        r[temp == l] = cmap[l,0]
        g[temp == l] = cmap[l,1]
        b[temp == l] = cmap[l,2]
    bgr = np.zeros((temp.shape[0], temp.shape[1], 3))
    bgr[:, :, 0] = b
    bgr[:, :, 1] = g
    bgr[:, :, 2] = r
    return bgr.astype(np.uint8)

class SemanticCloud:
    """
    Class for ros node to take in a color image (bgr) and do semantic segmantation on it to produce an image with semantic class colors (chair, desk etc.)
    Then produce point cloud based on depth information
    CNN: PSPNet (https://arxiv.org/abs/1612.01105) (with resnet50) pretrained on ADE20K, fine tuned on SUNRGBD or not
    """
    def __init__(self):
        """
        Constructor
        \param gen_pcl (bool) whether generate point cloud, if set to true the node will subscribe to depth image
        """
        # Get image size
        self.img_width, self.img_height = rospy.get_param('/camera/width'), rospy.get_param('/camera/height')
        # self.img_width, self.img_height = 240, 320
        
        # Set up CNN is use semantics
        
        print('Setting up CNN model...')
        # Set device
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        
        # Setup model
        model_dir = os.path.dirname(os.path.dirname(__file__))
        model_path = rospy.get_param('/semantic_pcl/model_path')
        #model_path = '/home/ywen/i2ros_2024/src/semantic_perception/model/RedLightSeg_BigSet_Mit.pth'
        self.n_classes = 2 # Semantic class number
        self.model = smp.FPN("resnet34", in_channels=3, classes=1)
        model_weights = torch.load(model_dir + model_path)

        new_model_weights = self.remove_heads(model_weights)
        # print(new_model_weights.keys())
        if 'mean' in new_model_weights:
           self.mean = (new_model_weights['mean'].numpy()).squeeze()
           self.std = (new_model_weights['std'].numpy()).squeeze()
        # print(self.mean)
        # print(self.std)
        self.model.load_state_dict(new_model_weights, strict=False)

        self.cnn_input_size = (320, 256)
        self.model = self.model.to(self.device)
        self.model.eval()
        self.cmap = color_map(N = self.n_classes, normalized = False) # Color map for semantic classes

        # Set up ROS
        print('Setting up ROS...')
        self.bridge = CvBridge() # CvBridge to transform ROS Image message to OpenCV image
        # Semantic image publisher
        self.sem_img_pub = rospy.Publisher("/semantic_image", Image, queue_size = 1)
        self.image_sub = rospy.Subscriber(rospy.get_param('/semantic_pcl/color_image_topic'), Image, self.color_callback, queue_size = 1, buff_size = 30*320*240)
        # self.image_sub = rospy.Subscriber( "/unity_ros/OurCar/Sensors/RGBCameraLeft/image_raw", Image, self.color_callback, queue_size = 1, buff_size = 30*320*240)
        print('Setting up ROS done!')
        rospy.set_param("/state_machine/init_done", True)
        
        
        

    def remove_heads(self, state_dict):
        new_state_dict = {}
        for key, value in state_dict.items():
            new_key = key.replace('model.', '')  
            new_state_dict[new_key] = value
        return new_state_dict
    
    def color_callback(self, color_img_ros):
        """
        Callback function for color image, de semantic segmantation and show the decoded image. For test purpose
        \param color_img_ros (sensor_msgs.Image) input ros color image message
        """
        try:
            color_img = self.bridge.imgmsg_to_cv2(color_img_ros, "bgr8") # Convert ros msg to numpy array
        except CvBridgeError as e:# Get dataset
            print(e)
        
        # Do semantic segmantation
        confidence, label= self.predict(color_img)

        
        confidence, label = confidence.squeeze(0).squeeze(0).cpu().numpy(), label.squeeze(0).squeeze(0).cpu().numpy()

        label = resize(label, (self.img_height, self.img_width), order = 0, mode = 'reflect', anti_aliasing=True, preserve_range = True) # order = 0, nearest neighbour
        label = label.astype(np.int16)
        # Add semantic class colors
        decoded = decode_segmap(label, self.n_classes, self.cmap)     # Show input image and decoded image
        confidence = resize(confidence, (self.img_height, self.img_width),  mode = 'reflect', anti_aliasing=True, preserve_range = True)
        # print(confidence.shape)
        # print(label.shape)
        # cv2.imshow('Camera image', color_img)
        # cv2.imshow('confidence', confidence)
        # cv2.imshow('Semantic segmantation', decoded)
        
        try:
            decoded_msg = self.bridge.cv2_to_imgmsg(decoded, encoding="bgr8")
            decoded_msg.header.stamp = color_img_ros.header.stamp
            decoded_msg.header.frame_id = color_img_ros.header.frame_id
            self.sem_img_pub.publish(decoded_msg)
        except CvBridgeError as e:
            print(e)



    def predict(self, img):
        """
        Do semantic segmantation
        \param img: (numpy array bgr8) The input cv image
        """
        img = img.copy() # Make a copy of image because the method will modify the image
        #orig_size = (img.shape[0], img.shape[1]) # Original image size
        # Prepare image: first resize to CNN input size
        img = resize(img, self.cnn_input_size, mode = 'reflect', anti_aliasing=True, preserve_range = True) # Give float64
        img = img.astype(np.float32)
        img = (img - self.mean) / self.std
        # Convert HWC -> CHW
        img = img.transpose(2, 0, 1)
        # Convert to tensor
        img = torch.tensor(img, dtype = torch.float32)
        img = img.unsqueeze(0) # Add batch dimension required by CNN
        with torch.no_grad():
            img = img.to(self.device)
            # Do inference
            probabilities = self.model(img).sigmoid() #N,C,W,H
            # Apply softmax to obtain normalized probabilities
            outputs = (probabilities > 0.4).float()
            # confidence 是每个像素属于目标类别的概率
            confidence = probabilities
            # 由于是二元分割，label 可以简单定义为 predictions
            label = outputs

            return confidence,label


def main(args):
    rospy.init_node('semantic_cloud', anonymous=True)
    seg_cnn = SemanticCloud()
    try:
        rospy.spin()
    except KeyboardInterrupt:
        print("Shutting down")

if __name__ == '__main__':
    main(sys.argv)
