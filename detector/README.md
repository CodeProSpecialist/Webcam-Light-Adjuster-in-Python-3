# The object detector

`object_detection_nanodet_2022nov.onnx` is NanoDet-Plus-m, taken from the
OpenCV Model Zoo (https://github.com/opencv/opencv_zoo), where it is published
under the Apache License 2.0 - the full text is in `LICENSE-nanodet.txt`
beside it. It is a data file: weights, not code. Nothing in this repository
runs code from it, and the decoding of its output is written out in
`webcam_detect.py` rather than imported from anywhere.

It is trained on COCO, so of its eighty classes these scripts use six:

    person, bicycle, car, motorcycle, bus, truck

3.8 MB, and it runs through `cv2.dnn`, which reads ONNX in both OpenCV 4 and
OpenCV 5. It needs no GPU and takes well under a second on a frame.

What it is for: finding the car or the person in the picture so that the
correction can be aimed at them, instead of spreading the same lift over the
sky and the tarmac as well. See "Finding cars and people" in the main README.
