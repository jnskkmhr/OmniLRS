__author__ = "Antoine Richard"
__copyright__ = (
    "Copyright 2023, Space Robotics Lab, SnT, University of Luxembourg, SpaceR"
)
__license__ = "GPL"
__version__ = "1.0.0"
__maintainer__ = "Antoine Richard"
__email__ = "antoine.richard@uni.lu"
__status__ = "development"

# This code is based on: https://github.com/morgan3d/misc/tree/master/terrain
# Original author: Morgan McGuire, http://cs.williams.edu/~morgan

from copy import copy
import warp as wp
import dataclasses
import numpy as np
import hashlib
import os


@wp.kernel
def _linear_interpolation(
    x: wp.array(dtype=float),
    y: wp.array(dtype=float),
    q11: wp.array(dtype=float),
    q12: wp.array(dtype=float),
    q21: wp.array(dtype=float),
    q22: wp.array(dtype=float),
    out: wp.array(dtype=float),
):
    tid = wp.tid()
    out[tid] = ((1.0 - x[tid]) * q11[tid] + x[tid] * q21[tid]) * (1.0 - y[tid]) + (
        (1.0 - x[tid]) * q12[tid] + x[tid] * q22[tid]
    ) * y[tid]


@dataclasses.dataclass
class GeoClipmapSpecs:
    numMeshLODLevels: int = 7
    meshBaseLODExtentHeightfieldTexels: int = 256
    meshBackBonePath: str = "terrain_mesh_backbone.npz"
    demPath: str = "40k_dem.npy"
    meters_per_pixel: float = 5.0
    meters_per_texel: float = 5.0
    z_scale: float = 1.0


def Point3(x, y, z):
    return np.array([x, y, z])


class GeoClipmap:
    def __init__(self, specs: GeoClipmapSpecs):
        self.specs = specs
        self.index_count = 0
        self.prev_indices = {}
        self.new_indices = {}
        self.points = []
        self.uvs = []
        self.indices = []

        self.specs_hash = self.compute_hash(self.specs)

        self.initMesh()
        self.loadDEM()
        self.initial_position = [0, 0]

    def gridIndex(self, x, y, stride):
        return y * stride + x

    @staticmethod
    def compute_hash(specs):
        return hashlib.sha256(str(specs).encode("utf-8")).hexdigest()

    def querryPointIndex(self, point):
        hash = str(point[::2])
        if hash in self.prev_indices.keys():
            index = self.prev_indices[hash]
        elif hash in self.new_indices.keys():
            index = self.new_indices[hash]
        else:
            index = copy(self.index_count)
            self.points.append(point)
            self.new_indices[hash] = index
            self.index_count += 1
        return index

    def addTriangle(self, A, B, C):
        A_idx = self.querryPointIndex(A)
        B_idx = self.querryPointIndex(B)
        C_idx = self.querryPointIndex(C)
        self.indices.append(A_idx)
        self.indices.append(B_idx)
        self.indices.append(C_idx)
        self.uvs.append(A[::2])
        self.uvs.append(B[::2])
        self.uvs.append(C[::2])

    def buildMesh(self):
        print("Building the mesh backbone, this may take time...")
        for level in range(0, self.specs.numMeshLODLevels):
            print(
                "Generating level "
                + str(level)
                + " out of "
                + str(self.specs.numMeshLODLevels)
                + "..."
            )
            step = 1 << level
            if level == 0:
                prevStep = 0
            else:
                prevStep = max(0, (1 << (level - 1)))
            halfStep = prevStep

            g = self.specs.meshBaseLODExtentHeightfieldTexels / 2
            L = float(level)

            # Pad by one element to hide the gap to the next level
            pad = 0
            radius = int(step * (g + pad))
            for z in range(-radius, radius, step):
                for x in range(-radius, radius, step):
                    if max(abs(x + halfStep), abs(z + halfStep)) >= g * prevStep:
                        # Cleared the cutout from the previous level. Tessellate the
                        # square.

                        #   A-----B-----C
                        #   | \   |   / |
                        #   |   \ | /   |
                        #   D-----E-----F
                        #   |   / | \   |
                        #   | /   |   \ |
                        #   G-----H-----I

                        A = Point3(float(x), L, float(z))
                        C = Point3(float(x + step), L, A[-1])
                        G = Point3(A[0], L, float(z + step))
                        I = Point3(C[0], L, G[-1])

                        B = (A + C) * 0.5
                        D = (A + G) * 0.5
                        F = (C + I) * 0.5
                        H = (G + I) * 0.5

                        E = (A + I) * 0.5

                        # Stitch the border into the next level

                        if x == -radius:
                            #   A-----B-----C
                            #   | \   |   / |
                            #   |   \ | /   |
                            #   |     E-----F
                            #   |   / | \   |
                            #   | /   |   \ |
                            #   G-----H-----I
                            self.addTriangle(E, A, G)
                        else:
                            self.addTriangle(E, A, D)
                            self.addTriangle(E, D, G)

                        if z == (radius - step):
                            self.addTriangle(E, G, I)
                        else:
                            self.addTriangle(E, G, H)
                            self.addTriangle(E, H, I)

                        if x == (radius - step):
                            self.addTriangle(E, I, C)
                        else:
                            self.addTriangle(E, I, F)
                            self.addTriangle(E, F, C)

                        if z == -radius:
                            self.addTriangle(E, C, A)
                        else:
                            self.addTriangle(E, C, B)
                            self.addTriangle(E, B, A)
            self.prev_indices = copy(self.new_indices)
            self.new_indices = {}
        self.points = np.array(self.points) * self.specs.meters_per_texel
        self.uvs = np.array(self.uvs) * self.specs.meters_per_texel
        self.indices = np.array(self.indices)

    def saveMesh(self):
        np.savez_compressed(
            self.specs.meshBackBonePath,
            points=self.points,
            indices=self.indices,
            uvs=self.uvs,
            specs_hash=self.specs_hash,
        )

    def loadMesh(self):
        data = np.load(self.specs.meshBackBonePath)
        if data["specs_hash"] != self.specs_hash:
            self.buildMesh()
            self.saveMesh()
        else:
            self.points = data["points"]
            self.indices = data["indices"]
            self.uvs = data["uvs"]

    def initMesh(self):
        # Cache the mesh backbone between runs because it is expensive to generate
        if os.path.exists(self.specs.meshBackBonePath):
            self.loadMesh()
        else:
            self.buildMesh()
            self.saveMesh()

    def loadDEM(self):
        self.dem = np.load(self.specs.demPath) * self.specs.z_scale
        self.dem = np.flipud(self.dem)
        self.dem_size = self.dem.shape

    def getElevation(self, position):
        position_in_pixel = position * (1.0 / self.specs.meters_per_pixel)
        self.wp_texel_per_pixel = (
            self.specs.meters_per_pixel / self.specs.meters_per_texel
        )
        x = (
            self.points[:, 0] / (self.specs.meters_per_texel / self.wp_texel_per_pixel)
        ) + position_in_pixel[0]
        y = (
            self.points[:, 2] / (self.specs.meters_per_texel / self.wp_texel_per_pixel)
        ) + position_in_pixel[2]

        x = np.minimum(x, self.dem.shape[0] - 1)
        y = np.minimum(y, self.dem.shape[1] - 1)
        x = np.maximum(x, 0)
        y = np.maximum(y, 0)

        x1 = np.trunc(x).astype(int)
        y1 = np.trunc(y).astype(int)

        x2 = np.minimum(x1 + 1, self.dem_size[0] - 1)
        y2 = np.minimum(y1 + 1, self.dem_size[1] - 1)
        dx = x - x1
        dy = y - y1

        # q11 = self.dem[x1, y1]
        # q12 = self.dem[x1, y2]
        # q21 = self.dem[x2, y1]
        # q22 = self.dem[x2, y2]

        q11 = self.dem[y1, x1]
        q12 = self.dem[y2, x1]
        q21 = self.dem[y1, x2]
        q22 = self.dem[y2, x2]

        z = wp.zeros(x.shape[0], dtype=float)
        with wp.ScopedTimer("linear_interpolation", active=True):
            wp.launch(
                kernel=_linear_interpolation,
                dim=x.shape[0],
                inputs=[
                    wp.array(dx, dtype=float),
                    wp.array(dy, dtype=float),
                    wp.array(q11, dtype=float),
                    wp.array(q12, dtype=float),
                    wp.array(q21, dtype=float),
                    wp.array(q22, dtype=float),
                    z,
                ],
            )
        self.points[:, 1] = self.points[:, -1]
        self.points[:, -1] = z.numpy()


if __name__ == "__main__":
    from matplotlib import pyplot as plt
    from mpl_toolkits.mplot3d import axes3d, Axes3D  # <-- Note the capitalization!

    wp.init()
    specs = GeoClipmapSpecs()
    clipmap = GeoClipmap(specs)
    with wp.ScopedTimer("render", active=True):
        clipmap.getElevation(np.array([8192 * 1, 0, 8192 * 1]))

    print(clipmap.points.shape)
    print(clipmap.indices.shape)
    print(clipmap.uvs.shape)
    ax = plt.figure().add_subplot(projection="3d")
    ax.scatter(clipmap.points[:, 0], clipmap.points[:, 1], clipmap.points[:, 2])
    # plt.scatter(points[:,0], points[:,2])
    # plt.axes("equal")
    plt.show()
