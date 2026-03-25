import plotly.graph_objects as go
import numpy as np

fig = go.Figure()

# Face circle
theta = np.linspace(0, 2 * np.pi, 200)
fig.add_trace(go.Scatter(
    x=np.cos(theta), y=np.sin(theta),
    mode='lines',
    line=dict(color='gold', width=4),
    fill='toself',
    fillcolor='lightyellow',
    name='face'
))

# Left eye
eye_theta = np.linspace(0, 2 * np.pi, 100)
fig.add_trace(go.Scatter(
    x=-0.3 + 0.1 * np.cos(eye_theta),
    y=0.3 + 0.1 * np.sin(eye_theta),
    mode='lines',
    line=dict(color='black', width=2),
    fill='toself',
    fillcolor='black',
    name='left eye'
))

# Right eye
fig.add_trace(go.Scatter(
    x=0.3 + 0.1 * np.cos(eye_theta),
    y=0.3 + 0.1 * np.sin(eye_theta),
    mode='lines',
    line=dict(color='black', width=2),
    fill='toself',
    fillcolor='black',
    name='right eye'
))

# Smile (arc from -pi to 0, shifted down)
smile_theta = np.linspace(np.pi + 0.3, 2 * np.pi - 0.3, 100)
fig.add_trace(go.Scatter(
    x=0.5 * np.cos(smile_theta),
    y=0.5 * np.sin(smile_theta) - 0.1,
    mode='lines',
    line=dict(color='black', width=4),
    name='smile'
))

fig.update_layout(
    title='😊 Smiley Face',
    xaxis=dict(visible=False, range=[-1.3, 1.3]),
    yaxis=dict(visible=False, range=[-1.3, 1.3], scaleanchor='x'),
    showlegend=False,
    plot_bgcolor='lightblue',
    width=500,
    height=500,
)

fig.show()
