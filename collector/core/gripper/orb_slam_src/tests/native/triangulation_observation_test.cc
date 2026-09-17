#ifdef NDEBUG
#undef NDEBUG
#endif
#include "TriangulationObservation.h"
#include <cassert>
#include <iostream>
#include <limits>
#include <vector>
struct Features {
    int NLeft = -1;
    const void* mpCamera2 = nullptr;
    std::vector<cv::KeyPoint> mvKeys, mvKeysUn, mvKeysRight;
    std::vector<float> mvuRight, mvLevelSigma2{1.f}, mvScaleFactors{1.f};
};
int main() {
    using namespace ORB_SLAM3;
    Features fish;
    fish.NLeft = 1005;
    fish.mpCamera2 = &fish;
    fish.mvKeys.resize(1005);
    fish.mvKeysRight.resize(1008);
    fish.mvuRight.resize(1005, -1.f);
    TriangulationObservation o;
    for (std::size_t i=0; i<2013; ++i) {
        assert(GetTriangulationObservation(fish,i,o));
        assert(o.rightCamera == (i>=1005));
        assert(!o.rectifiedStereo && o.rightCoordinate == -1.f);
    }
    assert(GetTriangulationObservation(fish,1803,o));
    assert(o.keypoint == &fish.mvKeysRight[798]);
    fish.mvuRight.clear(); // Dual-camera lookup must not touch this array.
    assert(GetTriangulationObservation(fish,1803,o));
    assert(GetTriangulationObservation(fish,0,o));
    assert(!GetTriangulationObservation(fish,2013,o));
    assert(!GetTriangulationObservation(fish,std::numeric_limits<std::size_t>::max(),o));
    fish.mvKeysRight[798].octave=-1;
    assert(!GetTriangulationObservation(fish,1803,o));
    fish.mvKeysRight[798].octave=1;
    assert(!GetTriangulationObservation(fish,1803,o));
    Features pin;
    pin.mvKeysUn.resize(2);
    pin.mvuRight={12.f,-1.f};
    assert(GetTriangulationObservation(pin,0,o) && o.rectifiedStereo && o.rightCoordinate==12.f);
    assert(GetTriangulationObservation(pin,1,o) && !o.rectifiedStereo);
    assert(!GetTriangulationObservation(pin,2,o));
    pin.mvuRight.clear();
    assert(!GetTriangulationObservation(pin,0,o));
    pin.NLeft=-2;
    assert(!GetTriangulationObservation(pin,0,o));
    std::cout << "PASS: all 2013 fisheye indices, original crash index 1803, empty stereo array, malformed indices/octaves, rectified stereo\n";
}
