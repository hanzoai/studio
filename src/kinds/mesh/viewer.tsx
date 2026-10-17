/**
 * The glTF viewer: three.js (MIT), its GLTFLoader and OrbitControls. Drag to
 * orbit, scroll or pinch to zoom, right-drag to pan; the model is framed on
 * load. Loaded only when a 3D tab is opened.
 */
import { SizableText, YStack } from '@hanzo/gui'
import { useEffect, useRef, useState } from 'react'
import { AmbientLight, Box3, Color, DirectionalLight, PerspectiveCamera, Scene, Vector3, WebGLRenderer } from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

import type { Stored } from '../../made.ts'
import { read, type Wire } from '../../wire.ts'

export default function Viewer({ wire, file, title }: { wire: Wire; file: Stored; title: string }) {
  const box = useRef<HTMLDivElement | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const host = box.current
    if (!host) return
    let live = true
    const renderer = new WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio))
    host.appendChild(renderer.domElement)
    renderer.domElement.setAttribute('aria-label', `${title}, a 3D model: drag to turn it, scroll to zoom`)
    renderer.domElement.setAttribute('role', 'img')
    const scene = new Scene()
    scene.background = new Color(0x111114)
    const camera = new PerspectiveCamera(40, 1, 0.01, 1000)
    scene.add(new AmbientLight(0xffffff, 1.1))
    const sun = new DirectionalLight(0xffffff, 2.2)
    sun.position.set(3, 5, 4)
    scene.add(sun)
    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableDamping = true
    const size = () => {
      const w = host.clientWidth || 1
      const h = host.clientHeight || 1
      renderer.setSize(w, h, false)
      camera.aspect = w / h
      camera.updateProjectionMatrix()
    }
    const watch = new ResizeObserver(size)
    watch.observe(host)
    size()
    let frame = 0
    const tick = () => {
      controls.update()
      renderer.render(scene, camera)
      frame = requestAnimationFrame(tick)
    }
    tick()
    read(wire, file.key)
      .then((b) => b.arrayBuffer())
      .then((buf) => new GLTFLoader().parseAsync(buf, ''))
      .then((gltf) => {
        if (!live) return
        scene.add(gltf.scene)
        const bounds = new Box3().setFromObject(gltf.scene)
        const centre = bounds.getCenter(new Vector3())
        const span = bounds.getSize(new Vector3()).length() || 1
        controls.target.copy(centre)
        camera.position.copy(centre).add(new Vector3(span * 0.6, span * 0.4, span * 0.9))
        camera.near = span / 100
        camera.far = span * 100
        camera.updateProjectionMatrix()
      })
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : String(e)))
    return () => {
      live = false
      cancelAnimationFrame(frame)
      watch.disconnect()
      controls.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    }
  }, [wire, file.key, title])
  return (
    <YStack flex={1} minH={240} position="relative">
      <div ref={box} style={{ position: 'absolute', inset: 0 }} />
      {error ? (
        <YStack position="absolute" inset={0} items="center" justify="center">
          <SizableText color="$soft">{error}</SizableText>
        </YStack>
      ) : null}
    </YStack>
  )
}
