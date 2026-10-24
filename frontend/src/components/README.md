# 前端组件目录

本目录包含前端视觉聊天功能所需的 React 组件。

## 组件列表

### CameraCapture.tsx
摄像头捕获组件，提供实时摄像头预览和拍照功能。

**功能特性:**
- 支持实时视频预览
- 点击拍照按钮截取当前帧
- 支持前置/后置摄像头切换
- 完善的错误处理（权限拒绝、设备未找到等）

**使用示例:**
```tsx
import CameraCapture from '@/components/CameraCapture';

<CameraCapture
  onCapture={(imageData) => console.log('拍照成功:', imageData)}
  maxImages={9}
  currentImageCount={0}
/>
```

### ImageUploader.tsx
图片上传组件，支持文件选择和摄像头拍照两种方式。

**功能特性:**
- 支持多图上传
- 文件大小和格式验证
- 支持摄像头拍照
- 错误提示

**使用示例:**
```tsx
import ImageUploader from '@/components/ImageUploader';

<ImageUploader
  onImagesChange={(images) => console.log('图片列表:', images)}
  maxImages={9}
  maxFileSize={10 * 1024 * 1024}
  acceptedFormats={['png', 'jpg', 'jpeg', 'gif', 'webp']}
/>
```

### ImagePreview.tsx
图片预览组件，提供缩略图查看和大图预览功能。

**功能特性:**
- 缩略图预览
- 点击查看大图
- 删除图片功能

**使用示例:**
```tsx
import ImagePreview from '@/components/ImagePreview';

<ImagePreview
  src="data:image/jpeg;base64,..."
  onRemove={() => console.log('删除图片')}
/>
```

## 依赖

- React 18+
- TypeScript
- Lucide React (图标)
- Tailwind CSS (样式)

## 注意事项

1. 摄像头功能需要浏览器支持 `navigator.mediaDevices.getUserMedia` API
2. 图片上传需要 HTTPS 环境（或 localhost）
3. 文件大小限制默认为 10MB，可在配置中修改
