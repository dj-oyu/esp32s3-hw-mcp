cmake_minimum_required(VERSION 3.16)

# A second, independent ESP-IDF project: the PIE examples. It shares nothing with the measurement
# experiment (experiments/pie-timing) except the knowledge in data/ that the kernels were written from.
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(pie_examples)
