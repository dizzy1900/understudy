import { useEffect } from "react";

import Scene from "./scene/Scene";
import Overlay from "./ui/Overlay";
import { installSpeech } from "./voice/speech";
import { connect } from "./ws";

export default function App() {
  useEffect(() => connect(), []);
  useEffect(() => {
    return installSpeech();
  }, []);

  return (
    <>
      <div className="scene-layer">
        <Scene />
      </div>
      <Overlay />
    </>
  );
}
