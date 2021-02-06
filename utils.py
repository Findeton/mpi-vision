import fastbook
from fastbook import *
import PIL

device = torch.device("cuda")

def list_folders(path):
  p = Path(path)
  return [p/f for f in os.listdir(path) if os.path.isdir(p/f)]

def list_files(path):
  p = Path(path)
  return [p/f for f in os.listdir(path) if os.path.isfile(p/f)]

def flatten(l):
  return [item for sublist in l for item in sublist]
  
def meshgrid_abs_torch(batch, height, width):
  """Construct a 2D meshgrid in the absolute (homogeneous) coordinates.

  Args:
    batch: batch size
    height: height of the grid
    width: width of the grid
  Returns:
    x,y grid coordinates [batch, 3, height, width]
  """
  xs = torch.linspace(0.0, width-1, width)
  ys = torch.linspace(0.0, height-1, height)
  ys, xs = torch.meshgrid(xs, ys)
  ones = torch.ones_like(xs).to(device)
  coords = torch.stack([xs.to(device), ys.to(device), ones], axis=0)
  return torch.unsqueeze(coords, 0).repeat(batch, 1, 1, 1)

def divide_safe_torch(num, den, name=None):
  eps = 1e-8
  den = den.to(torch.float32).to(device)
  den += eps * den.eq(torch.tensor(0, device = device, dtype=torch.float32))
  return torch.div(num.to(torch.float32), den)

def transpose_torch(rot):
  return torch.transpose(rot, len(rot.shape)-2, len(rot.shape)-1)

def inv_homography_torch(k_s, k_t, rot, t, n_hat, a):
  """Computes inverse homography matrix between two cameras via a plane.

  Args:
      k_s: intrinsics for source cameras, [..., 3, 3] matrices
      k_t: intrinsics for target cameras, [..., 3, 3] matrices
      rot: relative rotations between source and target, [..., 3, 3] matrices
      t: [..., 3, 1], translations from source to target camera. Mapping a 3D
        point p from source to target is accomplished via rot * p + t.
      n_hat: [..., 1, 3], plane normal w.r.t source camera frame
      a: [..., 1, 1], plane equation displacement
  Returns:
      homography: [..., 3, 3] inverse homography matrices (homographies mapping
        pixel coordinates from target to source).
  """
  rot_t =  transpose_torch(rot)
  k_t_inv = torch.inverse(k_t) 

  denom = a - torch.matmul(torch.matmul(n_hat, rot_t), t)
  numerator = torch.matmul(torch.matmul(torch.matmul(rot_t, t), n_hat), rot_t)
  inv_hom = torch.matmul(
      torch.matmul(k_s, rot_t + divide_safe_torch(numerator, denom)),
      k_t_inv)
  return inv_hom

def transform_points_torch(points, homography):
  """Transforms input points according to homography.

  Args:
      points: [..., H, W, 3]; pixel (u,v,1) coordinates.
      homography: [..., 3, 3]; desired matrix transformation
  Returns:
      output_points: [..., H, W, 3]; transformed (u,v,w) coordinates.
  """
  # Because the points have two additional dimensions as they vary across the
  # width and height of an image, we need to reshape to multiply by the
  # per-image homographies.
  points_orig_shape = points.shape
  points_reshaped_shape = list(homography.shape)
  points_reshaped_shape[-2] = -1

  points_reshaped = torch.reshape(points, points_reshaped_shape)
  transformed_points = torch.matmul(points_reshaped, transpose_torch(homography))
  transformed_points = torch.reshape(transformed_points, points_orig_shape)
  return transformed_points

def normalize_homogeneous_torch(points):
  """Converts homogeneous coordinates to regular coordinates.

  Args:
      points: [..., n_dims_coords+1]; points in homogeneous coordinates.
  Returns:
      points_uv_norm: [..., n_dims_coords];
          points in standard coordinates after dividing by the last entry.
  """
  uv = points[..., :-1]
  w = torch.unsqueeze(points[..., -1], -1)
  return divide_safe_torch(uv, w)


def bilinear_wrapper_torch(imgs, coords):
  """Wrapper around bilinear sampling function, handles arbitrary input sizes.

  Args:
    imgs: [..., H_s, W_s, C] images to resample
    coords: [..., H_t, W_t, 2], source pixel locations from which to copy
  Returns:
    [..., H_t, W_t, C] images after bilinear sampling from input.
  """
  # The bilinear sampling code only handles 4D input, so we'll need to reshape.
  init_dims = list(imgs.shape[:-3:])
  end_dims_img = list(imgs.shape[-3::])
  end_dims_coords = list(coords.shape[-3::])
  prod_init_dims = init_dims[0]
  for ix in range(1, len(init_dims)):
    prod_init_dims *= init_dims[ix]

  imgs = torch.reshape(imgs, [prod_init_dims] + end_dims_img)
  coords = torch.reshape(
      coords, [prod_init_dims] + end_dims_coords)
  # change image from (N, H, W, C) to (N, C, H, W)
  imgs = imgs.permute([0, 3, 1, 2])
  # TODO: resize coords from (0,1) to (-1, 1)
  coords2 = torch.Tensor([-1, -1]).to(device) + 2.0 * coords
  imgs_sampled = torch.nn.functional.grid_sample(imgs, coords2)
  # imgs_sampled = torch.div(2.0* (imgs_sampled0 + torch.Tensor([1.0, 1.0])).to(device), torch.Tensor([(x_max - x_min), (y_max - y_min)])).to(device)
  # permute back to (N, H, W, C)
  imgs = imgs.permute([0, 2, 3, 1])
  imgs_sampled = torch.reshape(
      imgs_sampled, init_dims + list(imgs_sampled.shape)[-3::])
  return imgs_sampled

def over_composite(rgbas):
  """Combines a list of RGBA images using the over operation.

  Combines RGBA images from back to front with the over operation.
  The alpha image of the first image is ignored and assumed to be 1.0.

  Args:
    rgbas: A list of [batch, H, W, 4] RGBA images, combined from back to front.
  Returns:
    Composited RGB image.
  """
  for i in range(len(rgbas)):
    rgb = rgbas[i][:, :, :, 0:3]
    alpha = rgbas[i][:, :, :, 3:]
    #print('rgb.shape', rgb.shape)
    #print('alpha.shape', alpha.shape)
    if i == 0:
      output = rgb
    else:
      rgb_by_alpha = rgb * alpha
      output = rgb_by_alpha + output * (1.0 - alpha)
  return output


def transform_plane_imgs_torch(imgs, pixel_coords_trg, k_s, k_t, rot, t, n_hat, a):
  """Transforms input imgs via homographies for corresponding planes.

  Args:
    imgs: are [..., H_s, W_s, C]
    pixel_coords_trg: [..., H_t, W_t, 3]; pixel (u,v,1) coordinates.
    k_s: intrinsics for source cameras, [..., 3, 3] matrices
    k_t: intrinsics for target cameras, [..., 3, 3] matrices
    rot: relative rotation, [..., 3, 3] matrices
    t: [..., 3, 1], translations from source to target camera
    n_hat: [..., 1, 3], plane normal w.r.t source camera frame
    a: [..., 1, 1], plane equation displacement
  Returns:
    [..., H_t, W_t, C] images after bilinear sampling from input.
      Coordinates outside the image are sampled as 0.
  """
  hom_t2s_planes = inv_homography_torch(k_s, k_t, rot, t, n_hat, a)
  #print("hom_t2s_planes ", L(hom_t2s_planes))
  pixel_coords_t2s = transform_points_torch(pixel_coords_trg, hom_t2s_planes)
  #print("pixel_coords_t2s ", L(pixel_coords_t2s))
  pixel_coords_t2s = normalize_homogeneous_torch(pixel_coords_t2s)
  #print("imgs shape", imgs.shape)
  #print("pixel_coords_trg shape", pixel_coords_trg.shape)
  # print("pixel_coords_t2s shape", pixel_coords_t2s.shape)

  # convert from [0-height-1, width -1] to [0-1, 0-1]
  height_t = pixel_coords_trg.shape[-3]
  width_t = pixel_coords_trg.shape[-2]
  pixel_coords_t2s = pixel_coords_t2s / torch.Tensor([height_t - 1, width_t - 1]).to(device)

  # print("pixel_coords_t2s ", L(pixel_coords_t2s))

  imgs_s2t = bilinear_wrapper_torch(imgs, pixel_coords_t2s)
  #print("imgs_s2t ", L(imgs_s2t))

  return imgs_s2t


def planar_transform_torch(imgs, pixel_coords_trg, k_s, k_t, rot, t, n_hat, a):
  """Transforms imgs, masks and computes dmaps according to planar transform.

  Args:
    imgs: are [L, B, H, W, C], typically RGB images per layer
    pixel_coords_trg: tensors with shape [B, H_t, W_t, 3];
        pixel (u,v,1) coordinates of target image pixels. (typically meshgrid)
    k_s: intrinsics for source cameras, [B, 3, 3] matrices
    k_t: intrinsics for target cameras, [B, 3, 3] matrices
    rot: relative rotation, [B, 3, 3] matrices
    t: [B, 3, 1] matrices, translations from source to target camera
       (R*p_src + t = p_tgt)
    n_hat: [L, B, 1, 3] matrices, plane normal w.r.t source camera frame
      (typically [0 0 1])
    a: [L, B, 1, 1] matrices, plane equation displacement
      (n_hat * p_src + a = 0)
  Returns:
    imgs_transformed: [L, ..., C] images in trg frame
  Assumes the first dimension corresponds to layers.
  """
  n_layers = list(imgs.shape)[0]
  rot_rep_dims = [n_layers]
  rot_rep_dims += [1 for _ in range(len(   list(k_s.shape)  ))]

  cds_rep_dims = [n_layers]
  cds_rep_dims += [1 for _ in range(len(  list(pixel_coords_trg.shape)  ))]

  k_s = torch.unsqueeze(k_s, 0).repeat(rot_rep_dims)
  k_t = torch.unsqueeze(k_t, 0).repeat(rot_rep_dims)
  t = torch.unsqueeze(t, 0).repeat(rot_rep_dims)
  rot = torch.unsqueeze(rot, 0).repeat(rot_rep_dims)
  pixel_coords_trg = torch.unsqueeze(pixel_coords_trg, 0).repeat(cds_rep_dims)

  imgs_trg = transform_plane_imgs_torch(
      imgs, pixel_coords_trg, k_s, k_t, rot, t, n_hat, a)
  return imgs_trg

  # And no subtle bug here fuckers!

def projective_forward_homography_torch(src_images, intrinsics, pose, depths):
  """Use homography for forward warping.

  Args:
    src_images: [layers, batch, height, width, channels]
    intrinsics: [batch, 3, 3]
    pose: [batch, 4, 4]
    depths: [layers, batch]
  Returns:
    proj_src_images: [layers, batch, height, width, channels]
  """
  n_layers, n_batch, height, width, _ = src_images.shape
  # Format for planar_transform code:
  # rot: relativplane_sweep_torch_onee rotation, [..., 3, 3] matrices
  # t: [B, 3, 1], translations from source to target camera (R*p_s + t = p_t)
  # n_hat: [L, B, 1, 3], plane normal w.r.t source camera frame [0,0,1]
  #        in our case
  # a: [L, B, 1, 1], plane equation displacement (n_hat * p_src + a = 0)
  rot = pose[:, :3, :3]
  t = pose[:, :3, 3:]
  n_hat = torch.Tensor([0., 0., 1.]).reshape([1,1,1,3]).to(device) # tf.constant([0., 0., 1.], shape=[1, 1, 1, 3])
  n_hat = n_hat.repeat([n_layers, n_batch, 1, 1])
  a = -torch.reshape(depths, [n_layers, n_batch, 1, 1])
  k_s = intrinsics
  k_t = intrinsics
  pixel_coords_trg =  meshgrid_abs_torch(n_batch, height, width).permute([0, 2, 3, 1])
  proj_src_images = planar_transform_torch(
      src_images, pixel_coords_trg, k_s, k_t, rot, t, n_hat, a)
  return proj_src_images

def mpi_render_view_torch(rgba_layers, tgt_pose, planes, intrinsics):
    """Render a target view from an MPI representation.

    Args:
      rgba_layers: input MPI [batch, height, width, #planes, 4]
      tgt_pose: target pose to render from [batch, 4, 4]
      planes: list of depth for each plane
      intrinsics: camera intrinsics [batch, 3, 3]
    Returns:
      rendered view [batch, height, width, 3]
    """
    batch_size, _, _ = list(tgt_pose.shape)
    depths = planes.reshape([len(planes), 1])
    depths = depths.repeat(1, batch_size)
    #print(rgba_layers.cpu().shape)
    # to [#planes, batch, height, width, 4]
    rgba_layers = rgba_layers.permute([3, 0, 1, 2, 4])
    proj_images = projective_forward_homography_torch(rgba_layers, intrinsics,
                                                   tgt_pose, depths)
    # proj_images is [#planes, batch, 4, height, width]
    # change to [#planes, batch, H, W, 4]
    proj_images = proj_images.permute([0, 1, 3, 4, 2])
    proj_images_list = []
    #print("proj_images.shape", proj_images.shape)
    for i in range(len(planes)):
      proj_images_list.append(proj_images[i])
    output_image = over_composite(proj_images_list) # same as tensorflow's version!
    return output_image


def inv_depths(start_depth, end_depth, num_depths):
    """Sample reversed, sorted inverse depths between a near and far plane.

    Args:
      start_depth: The first depth (i.e. near plane distance).
      end_depth: The last depth (i.e. far plane distance).
      num_depths: The total number of depths to create. start_depth and
          end_depth are always included and other depths are sampled
          between them uniformly according to inverse depth.
    Returns:
      The depths sorted in descending order (so furthest first). This order is
      useful for back to front compositing.
    """
    inv_start_depth = 1.0 / start_depth
    inv_end_depth = 1.0 / end_depth
    depths = [start_depth, end_depth]
    for i in range(1, num_depths - 1):
      fraction = float(i) / float(num_depths - 1)
      inv_depth = inv_start_depth + (inv_end_depth - inv_start_depth) * fraction
      depths.append(1.0 / inv_depth)
    depths = sorted(depths)
    return depths[::-1]

def list_folders(path):
  p = Path(path)
  return [p/f for f in os.listdir(path) if os.path.isdir(p/f)]

def open_image(fname, size=224, format=False):
    img = PIL.Image.open(fname).convert('RGB')
    if size is not None:
        img = img.resize((size, size))
    t = torch.Tensor(np.array(img)).to(device)
    # t.permute(2,0,1).float()/255.0
    if format:
      return t.float()/255.0
    return t

def preprocess_image_torch(image):
  """Preprocess the image for CNN input.

  Args:
    image: the input image in float [0, 1]
  Returns:
    A new image converted to float with range [-1, 1]
  """
  return image * 2 - 1

def deprocess_image_torch(image):
    """Undo the preprocessing.

    Args:
      image: the input image in float with range [-1, 1]
    Returns:
      A new image converted to uint8 [0, 255]
    """
    return (((image + 1.) / 2.) *255).type(torch.ByteTensor)



def pixel2cam_torch(depth, pixel_coords, intrinsics, is_homogeneous=True):
  """Transforms coordinates in the pixel frame to the camera frame.

  Args:
    depth: [batch, height, width]
    pixel_coords: homogeneous pixel coordinates [batch, 3, height, width]
    intrinsics: camera intrinsics [batch, 3, 3]
    is_homogeneous: return in homogeneous coordinates
  Returns:
    Coords in the camera frame [batch, 3 (4 if homogeneous), height, width]
  """
  batch, height, width = depth.shape
  depth = torch.reshape(depth, [batch, 1, -1])
  pixel_coords = torch.reshape(pixel_coords, [batch, 3, -1])
  cam_coords = torch.matmul(torch.inverse(intrinsics), pixel_coords) * depth
  if is_homogeneous:
    ones = torch.ones([batch, 1, height*width]).to(device)
    cam_coords = torch.cat([cam_coords, ones], axis=1)
  cam_coords = torch.reshape(cam_coords, [batch, -1, height, width])
  return cam_coords

def cam2pixel_torch(cam_coords, proj):
  """Transforms coordinates in a camera frame to the pixel frame.

  Args:
    cam_coords: [batch, 4, height, width]
    proj: [batch, 4, 4]
  Returns:
    Pixel coordinates projected from the camera frame [batch, height, width, 2]
  """
  batch, _, height, width = cam_coords.shape
  cam_coords = torch.reshape(cam_coords, [batch, 4, -1])
  unnormalized_pixel_coords = torch.matmul(proj, cam_coords)
  xy_u = unnormalized_pixel_coords[:, 0:2, :]
  z_u = unnormalized_pixel_coords[:, 2:3, :]
  pixel_coords = xy_u / (z_u + 1e-10)
  pixel_coords = torch.reshape(pixel_coords, [batch, 2, height, width])
  return pixel_coords.permute([0, 2, 3, 1])

def resampler_wrapper_torch(imgs, coords):
  """
  equivalent to tfa.image.resampler
  Args:
    imgs: [N, H, W, C] images to resample
    coords: [N, H, W, 2], source pixel locations from which to copy
  Returns:
    [N, H, W, C] sampled pixels
  """
  return torch.nn.functional.grid_sample(
      imgs.permute([0, 3, 1, 2]),             # change images from (N, H, W, C) to (N, C, H, W)
      torch.Tensor([-1, -1]).to(device) + 2.0 * coords   # resize coords from (0,1) to (-1, 1)
      ).permute([0, 2, 3, 1])                 # change result from (N, C, H, W) to (N, H, W, C)

def projective_inverse_warp_torch(
    img, depth, pose, intrinsics, ret_flows=False):
  """Inverse warp a source image to the target image plane based on projection.

  Args:
    img: the source image [batch, height_s, width_s, 3]
    depth: depth map of the target image [batch, height_t, width_t]
    pose: target to source camera transformation matrix [batch, 4, 4]
    intrinsics: camera intrinsics [batch, 3, 3]
    ret_flows: whether to return the displacements/flows as well
  Returns:
    Source image inverse warped to the target image plane [batch, height_t,
    width_t, 3]
  """
  batch, height, width, _ = img.shape
  # Construct pixel grid coordinates.
  pixel_coords = meshgrid_abs_torch(batch, height, width)

  # Convert pixel coordinates to the camera frame.
  cam_coords = pixel2cam_torch(depth, pixel_coords, intrinsics)

  # Construct a 4x4 intrinsic matrix.
  filler = torch.Tensor([[[0., 0., 0., 1.]]]).to(device)
  filler = filler.repeat(batch, 1, 1)
  intrinsics = torch.cat([intrinsics, torch.zeros([batch, 3, 1]).to(device)], axis=2)
  intrinsics = torch.cat([intrinsics, filler], axis=1)

  # Get a 4x4 transformation matrix from 'target' camera frame to 'source'
  # pixel frame.
  proj_tgt_cam_to_src_pixel = torch.matmul(intrinsics, pose)
  src_pixel_coords = cam2pixel_torch(cam_coords, proj_tgt_cam_to_src_pixel)

  #print(f'src_pixel_coords shape {src_pixel_coords.shape}')
  #print(f'src_pixel_coords {L(src_pixel_coords[:, :, :3,:])}')

  src_pixel_coords = ( src_pixel_coords + torch.Tensor([0.5, 0.5]).to(device) ) / torch.Tensor([height, width]).to(device)

  output_img = resampler_wrapper_torch(img, src_pixel_coords)
  if ret_flows:
    return output_img, src_pixel_coords - cam_coords
  else:
    return output_img

def plane_sweep_torch(img, depth_planes, pose, intrinsics):
  """Construct a plane sweep volume.

  Args:
    img: source image [batch, height, width, #channels]
    depth_planes: a list of depth values for each plane
    pose: target to source camera transformation [batch, 4, 4]
    intrinsics: camera intrinsics [batch, 3, 3]
  Returns:
    A plane sweep volume [batch, height, width, #planes*#channels]
  """
  batch, height, width, _ = img.shape
  plane_sweep_volume = []

  for depth in depth_planes:
    curr_depth = torch.zeros([batch, height, width], dtype=torch.float32).to(device) + depth
    warped_img = projective_inverse_warp_torch(img, curr_depth, pose, intrinsics)
    plane_sweep_volume.append(warped_img)
  plane_sweep_volume = torch.cat(plane_sweep_volume, axis=3)
  return plane_sweep_volume

def format_network_input_torch(self, ref_image, psv_src_images, ref_pose,
                           psv_src_poses, planes, intrinsics):
    """Format the network input (reference source image + PSV of the 2nd image).

    Args:
      ref_image: reference source image [batch, height, width, 3]
      psv_src_images: stack of source images (excluding the ref image)
                      [batch, height, width, 3*(num_source -1)]
      ref_pose: reference world-to-camera pose (where PSV is constructed)
                [batchprojective_inverse_warp_torch, 4, 4]
      psv_src_poses: input poses (world to camera) [batch, num_source-1, 4, 4]
      planes: list of scalar depth values for each plane
      intrinsics: camera intrinsics [batch, 3, 3]
    Returns:
      net_input: [batch, height, width, (num_source-1)*#planes*3 + 3]
    """
    num_psv_source = psv_src_poses.shape[1]
    net_input = []
    net_input.append(ref_image)
    for i in range(num_psv_source):
      curr_pose = torch.matmul(psv_src_poses[:, i], torch.inverse(ref_pose))
      curr_image = psv_src_images[:, :, :, i * 3:(i + 1) * 3]
      curr_psv = plane_sweep_torch(curr_image, planes, curr_pose, intrinsics)
      net_input.append(curr_psv)
    net_input = torch.cat(net_input, axis=3)
    return net_input

import torchvision
from fastai.vision import *
import matplotlib.pyplot as plt

# shows an image
# input:
#   image: [C, H, W] torch tensor with values in range 0-255
def show_torch_image(image):
  toPIL = torchvision.transforms.ToPILImage()
  # to PIL takes a C x H x W
  pil_img = toPIL(image.float()/255.0).convert("RGB")
  plt.imshow(pil_img)

def plane_sweep_torch_one(img, depth_planes, pose, intrinsics):
  """Construct a plane sweep volume.

  Args:
    img: source image [height, width, #channels]
    depth_planes: a list of depth values for each plane
    pose: target to source camera transformation [4, 4]
    intrinsics: camera intrinsics [3, 3]
  Returns:
    A plane sweep volume [height, width, #planes*#channels]
  """
  height = img.shape[0]
  width = img.shape[1]
  plane_sweep_volume = []

  for depth in depth_planes:
    curr_depth = torch.zeros([height, width], dtype=torch.float32).to(device) + depth
    warped_img = projective_inverse_warp_torch(torch.unsqueeze(img, 0), torch.unsqueeze(curr_depth, 0), torch.unsqueeze(pose, 0), torch.unsqueeze(intrinsics, 0))
    plane_sweep_volume.append(warped_img)
  plane_sweep_volume = torch.cat(plane_sweep_volume, axis=3)
  return plane_sweep_volume

def scale_intrinsics(intrinsics, height, width):
  """ scale intrinsics with the (height, width) factors
    Args:
      intrinsics: [3, 3]
      height: height or height ratio for the scaling
      width: width or width ratio for the scaling
  """
  return intrinsics * torch.Tensor([
    [width, 1.0, width],
    [0.0, height, height],
    [0.0, 0.0, 1.0]
  ]).to(device)


def resize_with_intrinsics_torch(image_path, intrinsics, height, width):
    """
    Args:
      image: PIL image
      intrinsics: [3, 3] pixel camera intrinsics where the last dim is [fx fy cx cy] * [width height width height]
      height: (int) height of output images
      width: (int) width of output images
    Returns:
      scaled_image
      scaled_intrinsics
    """
    img = PIL.Image.open(image_path).convert('RGB')
    input_height = img.height
    input_width = img.width
    
    scaled_pixel_intrinsics = scale_intrinsics(
        intrinsics,
        height / input_height,
        width / input_width,
    )
    scaled_image = img.resize((width, height))
    tensor_image = preprocess_image_torch(torch.Tensor(np.array(scaled_image)).to(device)/255.0)
    
    return tensor_image, scaled_pixel_intrinsics
