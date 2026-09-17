#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"

#include <opencv2/opencv.hpp>
#include <atomic>
#include <thread>
#include <iostream>
#include <string>
#include <vector>
#include <algorithm>
#include <dirent.h>
#include <cstdlib>
#include <unordered_map>
#include <stdexcept>
#include <cmath>
#include <fstream>
#include <signal.h>
#include <libgen.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/types.h>
#include <unistd.h>


struct InputArgs {
    std::string configPath;
    std::string dataDir;
    std::string outDir;
    int leftId;
    int rightId;
    int midId;
};

static char cmd;
static std::atomic<bool> runningFlag(true);

std::string GetParentDirectoryPath(const std::string& file_path) {
    size_t pos = file_path.find_last_of('/');
    return (pos != std::string::npos) ? file_path.substr(0, pos + 1) : "./";
}

namespace {
int LoadImagesList(const std::string& dataDir, 
    std::vector<std::string>& vstrImageLeft, std::vector<std::string>& vstrImageRight,
    std::vector<std::string>& vstrImgMid,
    std::vector<ull>& vTimeStereo, std::vector<ull>& vTimeMiddle, 
    int leftId = 0, int rightId = 1, int middleId = -1);

int CreateDirectoryIfNotExists(const char* path);
}

void signalHandler(int sig) {
    runningFlag.store(false);
}

void processData(InputArgs param, void* handle) {
    std::vector<std::string> vstrImageLeft;
    std::vector<std::string> vstrImageRight;
    std::vector<std::string> vstrImgMid;
    std::vector<ull> vTimeStereo;
    std::vector<ull> vTimeMiddle;
     
    if (EXIT_FAILURE == LoadImagesList(param.dataDir, 
            vstrImageLeft, vstrImageRight, vstrImgMid,
            vTimeStereo, vTimeMiddle, param.leftId, param.rightId, param.midId)) {
        runningFlag.store(false);
        printf("Load image failed\n");
        return;    
    } else {
        printf("Load image count: %zu\n", vTimeStereo.size());
    }

    // mkdir
    std::string leftOutPath, rightOutPath, midOutPath;
    if (param.leftId != -1) {
        leftOutPath = param.outDir + "/cam" + std::to_string(param.leftId);
        if (CreateDirectoryIfNotExists(leftOutPath.c_str())) {
            printf("Error: Create left cam result directory failed: %s\n", leftOutPath.c_str());
            exit(-1);
        }
    }
    if (param.rightId != -1) {
        rightOutPath = param.outDir + "/cam" + std::to_string(param.rightId);
        if (CreateDirectoryIfNotExists(rightOutPath.c_str())) {
            printf("Error: Create right cam result directory failed: %s\n", rightOutPath.c_str());
            exit(-1);
        }
    }
    if (param.midId != -1) {
        midOutPath = param.outDir + "/cam" + std::to_string(param.midId);
        if (CreateDirectoryIfNotExists(midOutPath.c_str())) {
            printf("Error: Create mid cam result directory failed: %s\n", midOutPath.c_str());
            exit(-1);
        }
    }

    cv::Mat imLeft, imRight, imMid;
    AtrakImage atrakImg, outImg;
    cv::Mat firstImg = cv::imread(vstrImageLeft[0], cv::IMREAD_GRAYSCALE);
    printf("Stereo image size read from data: W %d, H %d\n", firstImg.cols, firstImg.rows);
    if (param.midId > -1) {
        cv::Mat firstMidImg = cv::imread(vstrImageLeft[0], cv::IMREAD_GRAYSCALE);
        printf("Middle RGB image size read from data: W %d, H %d\n", firstImg.cols, firstImg.rows);
    }
    atrakImg.data = new uchar[FAYS_ATRAK_IMG_MAX_BYTES];
    outImg.data = new uchar[FAYS_ATRAK_IMG_MAX_BYTES];

    for(size_t ni = 0; ni < vstrImageLeft.size(); ni++) {
        if (!runningFlag.load()) break;

        // Read left and right images from file
        imLeft = cv::imread(vstrImageLeft[ni], cv::IMREAD_GRAYSCALE);
        imRight = cv::imread(vstrImageRight[ni], cv::IMREAD_GRAYSCALE);

        if (imLeft.empty()) {
            printf("Failed to load image at: %s\n", vstrImageLeft[ni].c_str());
            runningFlag.store(false);
            break;
        }

        if (imRight.empty()) {
            printf("Failed to load image at: %s\n", vstrImageRight[ni].c_str());
            runningFlag.store(false);
            break;
        }
        printf("Dataset img t: %llu\n", vTimeStereo[ni]);
        
        // process stereo images
        int num_of_cam = 2;
        uint leftBytes = imLeft.cols * imLeft.rows * imLeft.channels();
        atrakImg.bytes = num_of_cam * leftBytes;
        atrakImg.device_id = ATRAK_DEV_STEREO;
        atrakImg.height = num_of_cam * imLeft.rows;
        atrakImg.width = imLeft.cols;
        atrakImg.channel = imLeft.channels();
        atrakImg.step = imLeft.cols * imLeft.channels();
        atrakImg.timestamp = vTimeStereo[ni];
        memcpy(atrakImg.data, imLeft.data, leftBytes * sizeof(uchar));
        memcpy(atrakImg.data + leftBytes, imRight.data, imRight.rows * imRight.cols * sizeof(uchar));
        if (EXIT_SUCCESS != FAYS_VIK_Offline_ProcessFrame(handle, &atrakImg, &outImg)) {
            printf("Failed to process stereo frame at: %s\n", vstrImageLeft[ni].c_str());
            runningFlag.store(false);
            break;
        }
        cv::Mat outLeftMat(outImg.height / 2, outImg.width, CV_8UC3, outImg.data);
        cv::Mat outRightMat(outImg.height / 2, outImg.width, CV_8UC3, outImg.data + leftBytes * 3);
        cv::imwrite(leftOutPath + "/" + std::to_string(outImg.timestamp) + ".bmp", outLeftMat);
        cv::imwrite(rightOutPath + "/" + std::to_string(outImg.timestamp) + ".bmp", outRightMat);
    }

    for(size_t ni = 0; ni < vstrImgMid.size(); ni++) {
        imMid = cv::imread(vstrImgMid[ni], cv::IMREAD_GRAYSCALE);
        atrakImg.bytes = imMid.cols * imMid.rows;
        atrakImg.device_id = ATRAK_DEV_MIDDLE;
        atrakImg.height = imMid.rows;
        atrakImg.width = imMid.cols;
        atrakImg.channel = imMid.channels();
        atrakImg.step = imMid.cols * imMid.channels();
        atrakImg.timestamp = vTimeMiddle[ni];
        memcpy(atrakImg.data, imMid.data, atrakImg.bytes * sizeof(uchar));
        if (EXIT_SUCCESS != FAYS_VIK_Offline_ProcessFrame(handle, &atrakImg, &outImg)) {
            printf("Failed to process middle frame at: %s\n", vstrImgMid[ni].c_str());
            runningFlag.store(false);
            break;
        }
        cv::Mat outMat(outImg.height, outImg.width, CV_8UC3, outImg.data);
        cv::imwrite(midOutPath + "/" + std::to_string(outImg.timestamp) + ".bmp", outMat);
    }

    if (atrakImg.data) {
        delete [] atrakImg.data;
        atrakImg.data = nullptr;
    }
    if (outImg.data) {
        delete [] outImg.data;
        outImg.data = nullptr;
    }

    runningFlag.store(false);
}


int parseArgs(int argc, char const *argv[], InputArgs& param) {
    // 检查参数数量是否正确，不正确则打印完整使用说明
    if (argc != 7) {
        // 打印完整的参数说明到控制台
        printf("=============================================\n");
        printf("Offline Service Usage Instructions\n");
        printf("=============================================\n");
        printf("Usage: ./fays_vikit_offline <config_path> <data_dir> <result_dir> <left_id> <right_id> <middle_id>\n");
        printf("\nParameters Explanation:\n");
        printf("  1. config_path  - Path to the configuration file\n");
        printf("  2. data_dir     - Directory where the raw data is stored\n");
        printf("  3. result_dir   - Directory to save the results (output files)\n");
        printf("  4. left_id      - Unique ID of the left camera (-1: this camera data does NOT exist)\n");
        printf("  5. right_id     - Unique ID of the right camera (-1: this camera data does NOT exist)\n");
        printf("  6. middle_id    - Unique ID of the middle camera (-1: this camera data does NOT exist)\n");
        printf("=============================================\n");
        printf("args num %d\n", argc);
        return EXIT_FAILURE;
    }

    param.configPath = std::string(argv[1]);
    param.dataDir = std::string(argv[2]);
    param.outDir = std::string(argv[3]);
    param.leftId = std::stoi(std::string(argv[4]));
    param.rightId = std::stoi(std::string(argv[5]));
    param.midId = std::stoi(std::string(argv[6]));

    printf("Config path: %s\n", param.configPath.c_str());
    printf("Data directory: %s\n", param.dataDir.c_str());
    printf("Result directory: %s\n", param.outDir.c_str());
    printf("Left camera ID: %d\n", param.leftId);
    printf("Right camera ID: %d\n", param.rightId);
    printf("Middle camera ID: %d\n", param.midId);

    if (param.leftId < 0 && param.rightId > -1) {
        printf("ERROR: Only support stereo.\n");
        return EXIT_FAILURE;
    }

    if (param.rightId < 0 && param.leftId > -1) {
        printf("ERROR: Only support stereo.\n");
        return EXIT_FAILURE;
    }

    return EXIT_SUCCESS;
}


int main(int argc, char const *argv[])
{
    signal(SIGINT, signalHandler);
    signal(SIGTERM, signalHandler);
    
    InputArgs param;
    if (parseArgs(argc, argv, param) == EXIT_FAILURE) {
        printf("Error: wrong args, please check!!!\n");
        exit(-1);
    }

    void* handle = nullptr;     // Vikit offline handle
    if (EXIT_FAILURE == FAYS_VIK_Offline_CreateHandleWithConfig(&handle, param.configPath.c_str())) {
        return -1;
    }

    processData(param, handle);

    return 0;
}


namespace {
int LoadImagesList(const std::string& dataDir, 
    std::vector<std::string>& vstrImageLeft, std::vector<std::string>& vstrImageRight,
    std::vector<std::string>& vstrImgMid,
    std::vector<ull>& vTimeStereo, std::vector<ull>& vTimeMiddle, 
    int leftId, int rightId, int middleId) {
    
    vstrImageLeft.clear();
    vstrImageRight.clear();
    vstrImgMid.clear();
    vTimeStereo.clear();
    vTimeMiddle.clear();
    vstrImageLeft.reserve(30 * 60 * 60);
    vstrImageRight.reserve(30 * 60 * 60);
    vstrImgMid.reserve(30 * 60 * 60);
    vTimeStereo.reserve(30 * 60 * 60);
    vTimeMiddle.reserve(30 * 60 * 60);

    auto loadCameraDir = [](const std::string& camDir) -> std::vector<std::pair<ull, std::string>> {
        std::vector<std::pair<ull, std::string>> camFiles;
        DIR* dir = opendir(camDir.c_str());
        if (!dir) return camFiles;

        // 匿名函数：解析时间戳
        auto parseTimestamp = [](const std::string& fileName) -> ull {
            size_t dotPos = fileName.find_last_of('.');
            if (dotPos == std::string::npos || fileName.substr(dotPos) != ".bmp") {
                throw std::invalid_argument("invalid bmp file");
            }
            return std::stoull(fileName.substr(0, dotPos));
        };

        dirent* entry = nullptr;
        while ((entry = readdir(dir)) != nullptr) {
            std::string fileName = entry->d_name;
            if (fileName == "." || fileName == "..") continue;

            try {
                uint64_t ts = parseTimestamp(fileName);
                camFiles.emplace_back(ts, camDir + "/" + fileName);
            } catch (const std::exception& e) {
                std::cerr << "Warning: skip " << fileName << " - " << e.what() << std::endl;
                continue;
            }
        }
        closedir(dir);

        std::sort(camFiles.begin(), camFiles.end(), [](const auto& a, const auto& b) {
            return a.first < b.first;
        });

        return camFiles;
    };

    auto fileExists = [](const std::string& filePath) -> bool {
        // 高效检查文件存在性（不打开文件，仅检查状态）
        std::ifstream f(filePath.c_str());
        return f.good();
    };

    auto getFileNameFromPath = [](const std::string& fullPath) -> std::string {
        size_t slashPos = fullPath.find_last_of('/');
        return (slashPos != std::string::npos) ? fullPath.substr(slashPos + 1) : fullPath;
    };

    std::string leftCamDir = dataDir + "/cam" + std::to_string(leftId);
    std::vector<std::pair<ull, std::string>> leftFiles = loadCameraDir(leftCamDir);

    if (rightId != -1) {
        std::string rightCamDir = dataDir + "/cam" + std::to_string(rightId);
        std::for_each(leftFiles.begin(), leftFiles.end(), [&](const std::pair<ull, std::string>& leftPair) {
            const std::string& leftPath = leftPair.second;
            std::string leftFileName = getFileNameFromPath(leftPath);
            std::string rightPath = rightCamDir + "/" + leftFileName;

            if (fileExists(rightPath)) {
                printf("checked img: %s\n", leftPath.c_str());
                printf("checked img: %s\n", rightPath.c_str());
                vstrImageLeft.push_back(leftPath);
                vstrImageRight.push_back(rightPath);
                vTimeStereo.push_back(leftPair.first);
            }
        });
    } else if (rightId == -1) {
        std::for_each(leftFiles.begin(), leftFiles.end(), [&](const auto& leftPair) {
            vstrImageLeft.push_back(leftPair.second);
            vTimeStereo.push_back(leftPair.first);
        });
        vstrImageRight.clear();
    }

    if (middleId != -1) {
        std::string midCamDir = dataDir + "/cam" + std::to_string(middleId);
        auto midFiles = loadCameraDir(midCamDir);
        std::for_each(midFiles.begin(), midFiles.end(), [&](const auto& midPair) {
            vstrImgMid.push_back(midPair.second);
            vTimeMiddle.push_back(midPair.first);
        });
    }

    printf("Load image completed: left %zu right %zu mid %zu\n", vstrImageLeft.size(), vstrImageRight.size(), vstrImgMid.size());

    return EXIT_SUCCESS;
}

int CreateDirectoryIfNotExists(const char* path) {
    struct stat info;
    int statRC = stat(path, &info);
    if (statRC != 0) {
        if (errno == ENOENT) {
            printf("%s not exists, trying to create it\n", path);
            if (!CreateDirectoryIfNotExists(dirname(strdupa(path)))) {
                if (mkdir(path, S_IRWXU | S_IRWXG | S_IROTH | S_IXOTH)) {
                    fprintf(stderr, "Failed to create folder %s\n", path);
                    return 1;
                } else {
                    return 0;
                }
            } else {
                return 1;
            }
        } // directory not exists

        if (errno == ENOTDIR) {
            fprintf(stderr, "%s is not a directory path\n", path);
            return 1;
        } // something in path prefix is not a dir
        return 1;
    }
    return (info.st_mode & S_IFDIR) ? 0 : 1;
}

}